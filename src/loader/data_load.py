from typing import Dict, Any
from datetime import datetime
from io import StringIO

import polars as pl

from src.config import Config
from src.logger import setup_logger
from src.connection_manager import ConnectionManager
from src.metrics.pipeline_metrics import PipelineMetrics
from src.recover.recovery_manager import RecoveryManager
from src.tracker.metadata_tracker import MetadataTracker
from src.tracker.batch_tracker import BatchTracker
from .value_formatter import ValueFormatter
from .batch_processor import BatchProcessor
from .serializer import RecordSerializer


class DataLoader:
    def __init__(
        self,
        config: Config,
        metadata_tracker: MetadataTracker,
        recovery_manager: RecoveryManager,
    ):
        self.config = config
        self.metadata_tracker = metadata_tracker
        self.recovery_manager = recovery_manager
        self.batch_size = config.BATCH_SIZE
        self.logger = setup_logger("data_loader")

        # Initialize components
        self.conn_manager = ConnectionManager()
        self.value_formatter = ValueFormatter()
        self.batch_processor = BatchProcessor(self.value_formatter)
        self.serializer = RecordSerializer()
        self.batch_tracker = BatchTracker()

    async def initialize(self):
        """Initialize the data loader and its components"""
        await self.batch_tracker.initialize()

    async def close(self):
        """Nothing to close as we're using shared connection pool"""
        pass

    async def load_data_with_tracking(
        self,
        df: pl.DataFrame,
        table_name: str,
        primary_key: str,
        file_name: str,
        batch_id: str,
        merge_strategy: str,
        metrics: PipelineMetrics,
        schema: dict,
    ) -> None:
        """
        Load data into the target table while tracking metrics and managing errors.

        Args:
            df: Polars DataFrame containing the data to load
            table_name: Name of the target table
            primary_key: Primary key column name
            file_name: Name of the source file
            batch_id: Unique identifier for this batch
            merge_strategy: One of "INSERT", "UPDATE", or "MERGE"
            metrics: PipelineMetrics instance for tracking
            schema: Dictionary containing column schema information
        """
        start_time = datetime.now()

        try:
            # Load the data using the data loader
            total_processed, inserts, updates = await self.load_data(
                df=df,
                table_name=table_name,
                primary_key=primary_key,
                file_name=file_name,
                batch_id=batch_id,
                merge_strategy=merge_strategy,
            )

            # Update metrics
            metrics.rows_processed = total_processed
            metrics.rows_inserted = inserts
            metrics.rows_updated = updates
            # Calculate file size using StringIO buffer
            buffer = StringIO()
            df.write_csv(buffer)
            metrics.file_size_bytes = len(buffer.getvalue().encode("utf-8"))
            metrics.processing_status = "COMPLETED"

            # Remove duplicate log message - keep only one
            self.logger.info(
                f"Successfully loaded {
                    total_processed} rows into {table_name} "
                f"({inserts} inserts, {updates} updates)"
            )

        except Exception as e:
            metrics.error_message = str(e)
            metrics.processing_status = "FAILED"
            self.logger.error(
                f"Error loading data into {table_name}: {str(e)}", exc_info=True
            )
            raise

        finally:
            # Record timing information
            metrics.end_time = datetime.now()
            metrics.load_duration_seconds = (
                metrics.end_time - start_time
            ).total_seconds()

    async def load_data(
        self,
        df: pl.DataFrame,
        table_name: str,
        primary_key: str,
        file_name: str,
        batch_id: str,
        merge_strategy: str = "MERGE",
    ) -> tuple[int, int, int]:
        """Load data into the target table with the specified merge strategy"""
        self.logger.info(
            f"Starting load_data for table {
                table_name} with batch_id {batch_id}"
        )

        async with ConnectionManager.get_pool().acquire() as conn:
            columns = df.columns
            schema = await ConnectionManager.get_column_types(table_name)
            records = df.to_dicts()

            total_processed = 0
            total_inserts = 0
            total_updates = 0

            total_batches = (len(records) + self.batch_size -
                             1) // self.batch_size

            for batch_number, i in enumerate(
                range(0, len(records), self.batch_size), 1
            ):
                batch_records = records[i: i + self.batch_size]

                # Start batch tracking
                await self.batch_tracker.start_batch(
                    batch_id=batch_id,
                    table_name=table_name,
                    file_name=file_name,
                    batch_number=batch_number,
                    total_batches=total_batches,
                    records_in_batch=len(batch_records),
                )

                try:
                    batch_processed = 0
                    batch_inserts = 0
                    batch_updates = 0
                    batch_failed = 0

                    # Format the batch records
                    formatted_records = await self.batch_processor.process_batch(
                        conn,
                        table_name,
                        columns,
                        batch_records,
                        schema,
                        batch_number,
                        total_batches,
                    )

                    for record in formatted_records:
                        existing_record = await conn.fetchrow(
                            f"SELECT * FROM {table_name} WHERE {primary_key} = ${
                                1}::{schema.get(primary_key, 'text')}",
                            record[primary_key],
                        )

                        sql = self.batch_processor.generate_sql(
                            table_name, columns, schema, merge_strategy, primary_key
                        )

                        values = [record[col] for col in columns]
                        try:
                            await conn.execute(sql, *values)
                            batch_processed += 1

                            # Log the operation details
                            operation_type = "UPDATE" if existing_record else "INSERT"
                            self.logger.info(
                                f"Batch {batch_id}: {
                                    operation_type} record in {table_name} - "
                                f"PK: {record[primary_key]} - "

                            )

                            if existing_record:
                                batch_updates += 1
                            else:
                                batch_inserts += 1

                            # Record the change
                            await self.metadata_tracker.record_change(
                                conn,
                                table_name,
                                "UPDATE" if existing_record else "INSERT",
                                record,
                                batch_id,
                                primary_key,
                                file_name,
                                old_record=(
                                    dict(
                                        existing_record) if existing_record else None
                                ),
                            )

                        except Exception as e:
                            batch_failed += 1
                            self.logger.error(
                                f"Batch {batch_id}: Error loading record into {
                                    table_name} - "
                                f"PK: {record[primary_key]} - "
                                f"Values: {values} - "
                                f"Error: {str(e)}"
                            )
                            self.logger.error(f"SQL: {sql}")
                            raise

                    # Complete batch tracking
                    await self.batch_tracker.complete_batch(
                        batch_id=batch_id,
                        batch_number=batch_number,
                        records_processed=batch_processed,
                        records_inserted=batch_inserts,
                        records_updated=batch_updates,
                        records_failed=batch_failed,
                    )

                    # Update totals
                    total_processed += batch_processed
                    total_inserts += batch_inserts
                    total_updates += batch_updates

                    self.logger.info(
                        f"Completed batch {batch_number}/{total_batches}: "
                        f"Processed {batch_processed} records "
                        f"({batch_inserts} inserts, {batch_updates} updates)"
                    )

                except Exception as e:
                    # Record batch failure
                    await self.batch_tracker.complete_batch(
                        batch_id=batch_id,
                        batch_number=batch_number,
                        records_processed=batch_processed,
                        records_inserted=batch_inserts,
                        records_updated=batch_updates,
                        records_failed=len(batch_records) - batch_processed,
                        error_message=str(e),
                    )
                    raise

            self.logger.info(
                f"Completed loading {
                    total_processed} records into {table_name} "
                f"({total_inserts} inserts, {total_updates} updates)"
            )
            return total_processed, total_inserts, total_updates

    def serialize_record(self, record: Dict[str, Any]) -> Dict[str, Any]:
        """Delegate serialization to RecordSerializer"""
        return self.serializer.serialize_record(record)
