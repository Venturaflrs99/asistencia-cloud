import argparse
import json

import boto3
from botocore.exceptions import ClientError


def create_bucket(s3, name, region):
    try:
        s3.head_bucket(Bucket=name)
    except ClientError as exc:
        code = exc.response["Error"]["Code"]
        if code not in {"404", "NoSuchBucket", "NotFound"}:
            raise
        args = {"Bucket": name}
        if region != "us-east-1":
            args["CreateBucketConfiguration"] = {
                "LocationConstraint": region
            }
        s3.create_bucket(**args)

    s3.put_public_access_block(
        Bucket=name,
        PublicAccessBlockConfiguration={
            "BlockPublicAcls": True,
            "IgnorePublicAcls": True,
            "BlockPublicPolicy": True,
            "RestrictPublicBuckets": True,
        },
    )

    s3.put_bucket_ownership_controls(
        Bucket=name,
        OwnershipControls={
            "Rules": [{"ObjectOwnership": "BucketOwnerEnforced"}]
        },
    )

    s3.put_bucket_encryption(
        Bucket=name,
        ServerSideEncryptionConfiguration={
            "Rules": [
                {
                    "ApplyServerSideEncryptionByDefault": {
                        "SSEAlgorithm": "AES256"
                    }
                }
            ]
        },
    )

    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "DenyInsecureTransport",
                "Effect": "Deny",
                "Principal": "*",
                "Action": "s3:*",
                "Resource": [
                    f"arn:aws:s3:::{name}",
                    f"arn:aws:s3:::{name}/*",
                ],
                "Condition": {
                    "Bool": {"aws:SecureTransport": "false"}
                },
            }
        ],
    }
    s3.put_bucket_policy(
        Bucket=name,
        Policy=json.dumps(policy),
    )


def create_table(ddb, name, sort_key=None):
    keys = [{"AttributeName": "empleado_id", "KeyType": "HASH"}]
    attributes = [
        {"AttributeName": "empleado_id", "AttributeType": "S"}
    ]

    if sort_key:
        keys.append(
            {"AttributeName": sort_key, "KeyType": "RANGE"}
        )
        attributes.append(
            {"AttributeName": sort_key, "AttributeType": "S"}
        )

    try:
        ddb.create_table(
            TableName=name,
            KeySchema=keys,
            AttributeDefinitions=attributes,
            BillingMode="PAY_PER_REQUEST",
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ResourceInUseException":
            raise

    ddb.get_waiter("table_exists").wait(TableName=name)
    table = ddb.describe_table(TableName=name)["Table"]

    actual_keys = {
        (item["AttributeName"], item["KeyType"])
        for item in table["KeySchema"]
    }
    expected_keys = {
        (item["AttributeName"], item["KeyType"])
        for item in keys
    }
    if actual_keys != expected_keys:
        raise RuntimeError(f"Esquema incompatible en {name}")

    actual_types = {
        item["AttributeName"]: item["AttributeType"]
        for item in table["AttributeDefinitions"]
    }
    if any(
        actual_types.get(item["AttributeName"]) != "S"
        for item in attributes
    ):
        raise RuntimeError(f"Tipos incompatibles en {name}")

    billing = table.get("BillingModeSummary", {}).get(
        "BillingMode", "PROVISIONED"
    )
    if billing != "PAY_PER_REQUEST":
        ddb.update_table(
            TableName=name,
            BillingMode="PAY_PER_REQUEST",
        )
        ddb.get_waiter("table_exists").wait(TableName=name)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--collection", default="asistencia-facial")
    parser.add_argument("--profile")
    args = parser.parse_args()

    session = boto3.Session(
        profile_name=args.profile,
        region_name=args.region,
    )
    s3 = session.client("s3")
    ddb = session.client("dynamodb")
    rek = session.client("rekognition")

    create_bucket(s3, args.bucket, args.region)
    create_table(ddb, "Empleados")
    create_table(ddb, "Asistencias", "timestamp")

    try:
        rek.create_collection(CollectionId=args.collection)
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ResourceAlreadyExistsException":
            raise

    print(
        json.dumps(
            {
                "region": args.region,
                "bucket": args.bucket,
                "collection": args.collection,
                "tables": ["Empleados", "Asistencias"],
                "status": "Infraestructura preparada",
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()