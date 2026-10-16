"""Persistence for sessions, throttling, skips, executions, operations and backup metadata.

One DynamoDB table (pk, sk) with a `ttl` attribute. Every state change that must not race goes
through a conditional write, so retries and concurrent Lambda invocations are safe. MemoryStore
implements the same semantics for the tests and the local dev server.

Item kinds (pk / sk):
  SESSION    / <sid>                    revoked sessions (logout), expire with the session
  THROTTLE   / ip#<ip>#<window> | all#<window>   failed-login counters
  SKIP       / <schedule>#<occurrence>  status pending | consumed | cancelled
  EXEC       / <schedule>#<occurrence>  one per scheduled run; status started | done
  LOCK       / <name>                   owner + expiresAt
  OP         / <id>                     start/stop operations started from the dashboard
  BACKUP     / <id>                     id sorts by time: <UTC stamp>-<type>
  CACHE      / metrics                  last CPU/memory query, shared by viewers for 2 minutes
"""
import copy
import decimal
import threading


def _plain(value):
    """DynamoDB returns Decimals; the rest of the code wants int/float."""
    if isinstance(value, decimal.Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    return value


def _ddb(value):
    if isinstance(value, float):
        return decimal.Decimal(str(value))
    if isinstance(value, dict):
        return {k: _ddb(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_ddb(v) for v in value]
    return value


class DynamoStore:
    def __init__(self, table_name, region=None):
        import boto3

        self.table = boto3.resource("dynamodb", region_name=region).Table(table_name)
        self.errors = self.table.meta.client.exceptions

    def get(self, pk, sk):
        return _plain(self.table.get_item(Key={"pk": pk, "sk": sk}, ConsistentRead=True).get("Item"))

    def _conditional(self, fn, **kwargs):
        try:
            fn(**kwargs)
            return True
        except self.errors.ConditionalCheckFailedException:
            return False

    @staticmethod
    def _expect(expect, names, values):
        parts = []
        for i, (attr, want) in enumerate((expect or {}).items()):
            names[f"#e{i}"] = attr
            if want is None:
                parts.append(f"attribute_not_exists(#e{i})")
            else:
                values[f":e{i}"] = _ddb(want)
                parts.append(f"#e{i} = :e{i}")
        return " AND ".join(parts)

    def put(self, item, if_absent=False, expect=None):
        kwargs = {"Item": _ddb(item)}
        names, values = {}, {}
        cond = "attribute_not_exists(pk)" if if_absent else self._expect(expect, names, values)
        if cond:
            kwargs["ConditionExpression"] = cond
        if names:
            kwargs["ExpressionAttributeNames"] = names
        if values:
            kwargs["ExpressionAttributeValues"] = values
        return self._conditional(self.table.put_item, **kwargs)

    def update(self, pk, sk, changes, expect=None, must_exist=True):
        names, values, sets = {}, {}, []
        for i, (attr, value) in enumerate(changes.items()):
            names[f"#u{i}"] = attr
            values[f":u{i}"] = _ddb(value)
            sets.append(f"#u{i} = :u{i}")
        cond = self._expect(expect, names, values)
        if must_exist:
            cond = " AND ".join(filter(None, ["attribute_exists(pk)", cond]))
        kwargs = {"Key": {"pk": pk, "sk": sk}, "UpdateExpression": "SET " + ", ".join(sets),
                  "ExpressionAttributeNames": names, "ExpressionAttributeValues": values}
        if cond:
            kwargs["ConditionExpression"] = cond
        return self._conditional(self.table.update_item, **kwargs)

    def delete(self, pk, sk, expect=None):
        kwargs = {"Key": {"pk": pk, "sk": sk}}
        names, values = {}, {}
        cond = self._expect(expect, names, values)
        if cond:
            kwargs.update(ConditionExpression=cond, ExpressionAttributeNames=names)
            if values:
                kwargs["ExpressionAttributeValues"] = values
        return self._conditional(self.table.delete_item, **kwargs)

    def acquire_lock(self, name, owner, now, ttl_seconds):
        """Take the lock if free or expired (not re-entrant)."""
        return self._conditional(
            self.table.put_item,
            Item={"pk": "LOCK", "sk": name, "owner": owner, "expiresAt": int(now + ttl_seconds),
                  "ttl": int(now + ttl_seconds + 3600)},
            ConditionExpression="attribute_not_exists(pk) OR expiresAt < :now",
            ExpressionAttributeValues={":now": int(now)})

    def release_lock(self, name, owner):
        return self.delete("LOCK", name, expect={"owner": owner})

    def increment(self, pk, sk, ttl):
        resp = self.table.update_item(
            Key={"pk": pk, "sk": sk}, UpdateExpression="ADD #c :one SET #t = :ttl",
            ExpressionAttributeNames={"#c": "count", "#t": "ttl"},
            ExpressionAttributeValues={":one": 1, ":ttl": int(ttl)}, ReturnValues="UPDATED_NEW")
        return int(resp["Attributes"]["count"])

    def query(self, pk, newest_first=True, limit=None, start_after=None):
        from boto3.dynamodb.conditions import Key

        kwargs = {"KeyConditionExpression": Key("pk").eq(pk), "ScanIndexForward": not newest_first,
                  "ConsistentRead": True}
        if limit:
            kwargs["Limit"] = limit
        if start_after:
            kwargs["ExclusiveStartKey"] = {"pk": pk, "sk": start_after}
        items = []
        while True:
            resp = self.table.query(**kwargs)
            items += [_plain(i) for i in resp.get("Items", [])]
            last = resp.get("LastEvaluatedKey")
            if limit or not last:
                return items, (last or {}).get("sk")
            kwargs["ExclusiveStartKey"] = last


class MemoryStore:
    """Same semantics as DynamoStore, in memory (tests, dev server)."""

    def __init__(self):
        self.items = {}
        self.lock = threading.Lock()

    def _matches(self, item, expect):
        for attr, want in (expect or {}).items():
            if want is None:
                if item is not None and attr in item:
                    return False
            elif item is None or item.get(attr) != want:
                return False
        return True

    def get(self, pk, sk):
        return copy.deepcopy(self.items.get((pk, sk)))

    def put(self, item, if_absent=False, expect=None):
        with self.lock:
            key = (item["pk"], item["sk"])
            current = self.items.get(key)
            if if_absent and current is not None:
                return False
            if not self._matches(current, expect):
                return False
            self.items[key] = copy.deepcopy(item)
            return True

    def update(self, pk, sk, changes, expect=None, must_exist=True):
        with self.lock:
            current = self.items.get((pk, sk))
            if (must_exist and current is None) or not self._matches(current, expect):
                return False
            item = current or {"pk": pk, "sk": sk}
            item.update(copy.deepcopy(changes))
            self.items[(pk, sk)] = item
            return True

    def delete(self, pk, sk, expect=None):
        with self.lock:
            current = self.items.get((pk, sk))
            if not self._matches(current, expect):
                return False
            self.items.pop((pk, sk), None)
            return True

    def acquire_lock(self, name, owner, now, ttl_seconds):
        with self.lock:
            current = self.items.get(("LOCK", name))
            if current and current["expiresAt"] >= now:
                return False
            self.items[("LOCK", name)] = {"pk": "LOCK", "sk": name, "owner": owner, "expiresAt": int(now + ttl_seconds)}
            return True

    def release_lock(self, name, owner):
        return self.delete("LOCK", name, expect={"owner": owner})

    def increment(self, pk, sk, ttl):
        with self.lock:
            item = self.items.setdefault((pk, sk), {"pk": pk, "sk": sk, "count": 0})
            item["count"] += 1
            item["ttl"] = int(ttl)
            return item["count"]

    def query(self, pk, newest_first=True, limit=None, start_after=None):
        keys = sorted((k for k in self.items if k[0] == pk), key=lambda k: k[1], reverse=newest_first)
        if start_after is not None:
            keys = [k for k in keys if (k[1] < start_after if newest_first else k[1] > start_after)]
        page = keys[:limit] if limit else keys
        more = bool(limit) and len(keys) > limit
        return [copy.deepcopy(self.items[k]) for k in page], (page[-1][1] if more else None)
