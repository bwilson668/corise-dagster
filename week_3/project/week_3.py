from typing import List

from dagster import (
    In,
    Nothing,
    Out,
    ResourceDefinition,
    RetryPolicy,
    RunRequest,
    ScheduleDefinition,
    SkipReason,
    graph,
    op,
    sensor,
    static_partitioned_config,
)
from project.resources import mock_s3_resource, redis_resource, s3_resource
from project.sensors import get_s3_keys
from project.types import Aggregation, Stock


@op(
    config_schema={"s3_key": str},
    required_resource_keys={"s3"},
    out={"stocks": Out(dagster_type=List[Stock], description="List of Stocks")},
)
def get_s3_data(context):
    stocks = context.resources.s3.get_data(context.op_config["s3_key"])
    return [Stock.from_list(stock) for stock in stocks]


@op(
    ins={"stocks": In(dagster_type=List[Stock])},
    out={"aggregations": Out(dagster_type=Aggregation)},
    tags={"kind": "aggregate"},
    description="Aggregate stock data",
)
def process_data(stocks: List[Stock]) -> Aggregation:
    return Aggregation(date=stocks[2].date, high=max(stock.high for stock in stocks))


@op(
    ins={"aggregations": In(dagster_type=Aggregation)},
    required_resource_keys={"redis"},
    out=Out(Nothing),
    tags={"kind": "redis"},
    description="Store aggregations in Redis",
)
def put_redis_data(context, aggregations: Aggregation):
    context.resources.redis.put_data(str(aggregations.date), str(aggregations.high))


@graph
def week_3_pipeline():
    put_redis_data(process_data(get_s3_data()))


local = {
    "ops": {"get_s3_data": {"config": {"s3_key": "prefix/stock_9.csv"}}},
}

docker_resource_config = {
    "resources": {
        "s3": {
            "config": {
                "bucket": "dagster",
                "access_key": "test",
                "secret_key": "test",
                "endpoint_url": "http://localstack:4566",
            }
        },
        "redis": {
            "config": {
                "host": "redis",
                "port": 6379,
            }
        },
    },
}

docker = {
    **docker_resource_config,
    "ops": {"get_s3_data": {"config": {"s3_key": "prefix/stock_9.csv"}}},
}


@static_partitioned_config(partition_keys=[str(x) for x in range(1, 11)])
def docker_config(partition_key: str):
    return {
        **docker_resource_config,
        "ops": {"get_s3_data": {"config": {"s3_key": f"prefix/stock_{partition_key}.csv"}}},
    }


local_week_3_pipeline = week_3_pipeline.to_job(
    name="local_week_3_pipeline",
    config=local,
    resource_defs={
        "s3": mock_s3_resource,
        "redis": ResourceDefinition.mock_resource(),
    },
)

docker_week_3_pipeline = week_3_pipeline.to_job(
    name="docker_week_3_pipeline",
    config=docker_config,
    resource_defs={
        "s3": s3_resource,
        "redis": redis_resource,
    },
    op_retry_policy=RetryPolicy(max_retries=10, delay=1),
)


local_week_3_schedule = ScheduleDefinition(
    job=local_week_3_pipeline,
    name="local_week_3_schedule",
    cron_schedule="*/15 * * * *",
    run_config=local,
)

docker_week_3_schedule = ScheduleDefinition(
    job=docker_week_3_pipeline,
    name="docker_week_3_schedule",
    cron_schedule="0 * * * *",
    run_config=docker,
)


@sensor(
    job=docker_week_3_pipeline,
    minimum_interval_seconds=30,
)
def docker_week_3_sensor(context):
    s3_keys = get_s3_keys(
        bucket="dagster",
        prefix="prefix",
        endpoint_url="http://localstack:4566",
    )
    if not s3_keys:
        yield SkipReason("No new s3 files found in bucket.")

    for s3_key in s3_keys:
        yield RunRequest(
            run_key=s3_key,
            run_config={
                **docker_resource_config,
                "ops": {"get_s3_data": {"config": {"s3_key": s3_key}}},
            },
        )
