"""Thin AWS wrapper. Everything the dashboard does in AWS goes through Cloud, so the tests can
swap in a fake. Resource names come from the Lambda environment, never from the browser.

Environment (set by dashboard/deploy.sh):
  BUCKET, BUCKET_REGION   backup bucket (world/, downloads/, dashboard/)
  INSTANCE_NAME           Name tag of the game server instance
  TABLE                   DynamoDB table
  LOG_GROUP               CloudWatch log group the instance ships to
  SSM_DOCUMENT            the fixed backup command document
  SCHEDULE_GROUP          EventBridge Scheduler group
  AWS_LAMBDA_FUNCTION_NAME  set by Lambda; used to hand long tasks to an async invocation
"""
import json
import os
import time


class Cloud:
    def __init__(self, env=os.environ):
        import boto3
        from botocore.config import Config as BotoConfig

        cfg = BotoConfig(retries={"max_attempts": 3, "mode": "standard"}, connect_timeout=3, read_timeout=10)
        self.env = env
        self.region = env.get("AWS_REGION")
        self.bucket = env["BUCKET"]
        self.name = env.get("INSTANCE_NAME", "enshrouded")
        self.ec2 = boto3.client("ec2", config=cfg)
        self.s3 = boto3.client("s3", region_name=env["BUCKET_REGION"], config=cfg)
        self.ssm = boto3.client("ssm", config=cfg)
        self.logs = boto3.client("logs", config=cfg)
        self.scheduler = boto3.client("scheduler", config=cfg)
        self.lambda_ = boto3.client("lambda", config=cfg)

    # --- EC2 -------------------------------------------------------------------------------
    def instance(self):
        resp = self.ec2.describe_instances(Filters=[
            {"Name": "tag:Name", "Values": [self.name]},
            {"Name": "instance-state-name", "Values": ["pending", "running", "stopping", "stopped", "shutting-down"]},
        ])
        found = [i for r in resp["Reservations"] for i in r["Instances"]]
        if not found:
            return None
        i = max(found, key=lambda x: x["LaunchTime"])
        return {
            "id": i["InstanceId"],
            "state": i["State"]["Name"],
            "type": i["InstanceType"],
            "launchTime": i["LaunchTime"].timestamp(),
            "publicIp": i.get("PublicIpAddress"),
            "region": self.ec2.meta.region_name,
            "securityGroups": [g["GroupId"] for g in i.get("SecurityGroups", [])],
        }

    def start_instance(self, instance_id):
        self.ec2.start_instances(InstanceIds=[instance_id])

    def stop_instance(self, instance_id):
        self.ec2.stop_instances(InstanceIds=[instance_id])

    def elastic_ip(self):
        resp = self.ec2.describe_addresses(Filters=[{"Name": "tag:Name", "Values": [self.name]}])
        return resp["Addresses"][0]["PublicIp"] if resp["Addresses"] else None

    def udp_open_to_all(self, group_ids):
        if not group_ids:
            return None
        ports = set()
        for g in self.ec2.describe_security_groups(GroupIds=group_ids)["SecurityGroups"]:
            for p in g["IpPermissions"]:
                world = any(r.get("CidrIp") == "0.0.0.0/0" for r in p.get("IpRanges", []))
                if world and p.get("IpProtocol") in ("udp", "-1") and "FromPort" in p and p["ToPort"] - p["FromPort"] <= 16:
                    ports.update(range(p["FromPort"], p["ToPort"] + 1))
        return sorted(ports)

    # --- Backups on the instance (SSM Run Command, fixed document) ----------------------------
    def send_backup(self, instance_id, backup_id, backup_type, stop_server):
        resp = self.ssm.send_command(
            InstanceIds=[instance_id], DocumentName=self.env["SSM_DOCUMENT"], TimeoutSeconds=60,
            Parameters={"BackupId": [backup_id], "Type": [backup_type], "StopServer": ["yes" if stop_server else "no"]},
            Comment=f"dashboard {backup_type} backup {backup_id}")
        return resp["Command"]["CommandId"]

    def command_result(self, command_id, instance_id):
        """(status, stdout, stderr); status is Pending/InProgress/Success/Failed/... or 'Unknown'."""
        try:
            r = self.ssm.get_command_invocation(CommandId=command_id, InstanceId=instance_id)
        except self.ssm.exceptions.InvocationDoesNotExist:
            return "Unknown", "", ""
        return r["Status"], r.get("StandardOutputContent", ""), r.get("StandardErrorContent", "")

    def ssm_online(self, instance_id):
        resp = self.ssm.describe_instance_information(Filters=[{"Key": "InstanceIds", "Values": [instance_id]}])
        info = resp.get("InstanceInformationList") or []
        return bool(info) and info[0].get("PingStatus") == "Online"

    # --- S3 ---------------------------------------------------------------------------------
    def head(self, key):
        from botocore.exceptions import ClientError

        try:
            r = self.s3.head_object(Bucket=self.bucket, Key=key)
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
                return None
            raise
        return {"size": r["ContentLength"], "metadata": r.get("Metadata", {})}

    def list_objects(self, prefix):
        out = []
        for page in self.s3.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix=prefix):
            for o in page.get("Contents", []):
                out.append({"key": o["Key"], "size": o["Size"], "lastModified": o["LastModified"].timestamp()})
        return out

    def delete_objects(self, keys):
        for i in range(0, len(keys), 1000):
            chunk = keys[i:i + 1000]
            if chunk:
                self.s3.delete_objects(Bucket=self.bucket, Delete={"Objects": [{"Key": k} for k in chunk], "Quiet": True})

    def presign(self, key, filename, seconds=300):
        return self.s3.generate_presigned_url("get_object", ExpiresIn=seconds, Params={
            "Bucket": self.bucket, "Key": key,
            "ResponseContentDisposition": f'attachment; filename="{filename}"'})

    def read_json(self, key):
        try:
            return json.loads(self.s3.get_object(Bucket=self.bucket, Key=key)["Body"].read())
        except self.s3.exceptions.NoSuchKey:
            return None

    # --- Logs -------------------------------------------------------------------------------
    def log_events(self, stream_prefix, start_ms, end_ms, max_events):
        """Newest-last events from LOG_GROUP, bounded by time range and count."""
        kwargs = {"logGroupName": self.env["LOG_GROUP"], "startTime": int(start_ms), "endTime": int(end_ms),
                  "logStreamNamePrefix": stream_prefix, "limit": min(max_events, 1000)}
        events = []
        deadline = time.time() + 8
        while len(events) < max_events and time.time() < deadline:
            resp = self.logs.filter_log_events(**kwargs)
            events += [{"t": e["timestamp"], "m": e["message"]} for e in resp.get("events", [])]
            if not resp.get("nextToken"):
                break
            kwargs["nextToken"] = resp["nextToken"]
        return events

    # --- Scheduler --------------------------------------------------------------------------
    def get_schedule(self, name):
        try:
            return self.scheduler.get_schedule(GroupName=self.env["SCHEDULE_GROUP"], Name=name)
        except self.scheduler.exceptions.ResourceNotFoundException:
            return None

    # --- Async work -------------------------------------------------------------------------
    def run_async(self, payload):
        self.lambda_.invoke(FunctionName=self.env["AWS_LAMBDA_FUNCTION_NAME"], InvocationType="Event",
                            Payload=json.dumps(payload).encode())
