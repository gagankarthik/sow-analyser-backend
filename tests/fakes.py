"""In-memory stand-ins for AWS and OpenAI, shared by the test suite.

Nothing here opens a socket. ``FakeTable`` implements the slice of the DynamoDB
Table API the code uses (get / put with conditions / update / delete / query on
the table and on GSI1 / batch get), faithfully enough that the access-control
tests exercise the real key schema rather than a mock of it.
"""
from __future__ import annotations

import copy
import re
import types
from typing import Any

from botocore.exceptions import ClientError


def conditional_failure(op: str = "PutItem") -> ClientError:
    return ClientError({"Error": {"Code": "ConditionalCheckFailedException", "Message": "failed"}}, op)


_SERIALIZER = None


def assert_storable(value: Any) -> None:
    """Fail exactly as real DynamoDB would: boto3 refuses Python floats, and
    DynamoDB refuses empty sets. Run on everything the code under test writes,
    so a type that only breaks in production breaks the test instead."""
    global _SERIALIZER
    if _SERIALIZER is None:
        from boto3.dynamodb.types import TypeSerializer
        _SERIALIZER = TypeSerializer()
    _SERIALIZER.serialize(value)


def _eval(cond: Any, item: dict[str, Any]) -> bool:
    """Evaluate a boto3 ``Key(...)`` condition against an item."""
    expr = cond.get_expression()
    op, values = expr["operator"], expr["values"]
    if op == "AND":
        return _eval(values[0], item) and _eval(values[1], item)
    name = values[0].name
    if op == "=":
        return item.get(name) == values[1]
    if op == "begins_with":
        return str(item.get(name, "")).startswith(values[1])
    if name not in item:
        return False
    if op == "<=":
        return item[name] <= values[1]
    if op == "<":
        return item[name] < values[1]
    if op == ">=":
        return item[name] >= values[1]
    if op == ">":
        return item[name] > values[1]
    if op == "BETWEEN":
        return values[1] <= item[name] <= values[2]
    raise NotImplementedError(op)


class FakeTable:
    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict[str, Any]] = {}
        self.calls: list[tuple[str, Any]] = []

    # -- helpers ------------------------------------------------------------
    @staticmethod
    def _key(key: dict[str, Any]) -> tuple[str, str]:
        return key["PK"], key["SK"]

    def _check(self, condition: str | None, existing: dict[str, Any] | None, values: dict[str, Any] | None) -> None:
        if not condition:
            return
        for part in [c.strip() for c in condition.split(" AND ")]:
            m = re.fullmatch(r"attribute_not_exists\((\w+)\)", part)
            if m:
                if existing is not None and m.group(1) in existing:
                    raise conditional_failure()
                continue
            m = re.fullmatch(r"attribute_exists\((\w+)\)", part)
            if m:
                if existing is None or m.group(1) not in existing:
                    raise conditional_failure("UpdateItem")
                continue
            m = re.fullmatch(r"(\w+) = (:\w+)", part)
            if m:
                if existing is None or existing.get(m.group(1)) != (values or {})[m.group(2)]:
                    raise conditional_failure()
                continue
            raise NotImplementedError(part)

    # -- Table API ----------------------------------------------------------
    def get_item(self, Key: dict[str, Any], **_: Any) -> dict[str, Any]:
        self.calls.append(("get_item", Key))
        item = self.items.get(self._key(Key))
        return {"Item": copy.deepcopy(item)} if item else {}

    def put_item(self, Item: dict[str, Any], ConditionExpression: str | None = None,
                 ExpressionAttributeValues: dict[str, Any] | None = None, **_: Any) -> dict[str, Any]:
        self.calls.append(("put_item", Item.get("PK")))
        key = self._key(Item)
        assert_storable(Item)
        self._check(ConditionExpression, self.items.get(key), ExpressionAttributeValues)
        self.items[key] = copy.deepcopy(Item)
        return {}

    def delete_item(self, Key: dict[str, Any], **_: Any) -> dict[str, Any]:
        self.calls.append(("delete_item", Key))
        self.items.pop(self._key(Key), None)
        return {}

    def update_item(self, Key: dict[str, Any], UpdateExpression: str,
                    ExpressionAttributeValues: dict[str, Any] | None = None,
                    ExpressionAttributeNames: dict[str, str] | None = None,
                    ConditionExpression: str | None = None, **_: Any) -> dict[str, Any]:
        self.calls.append(("update_item", Key))
        key = self._key(Key)
        existing = self.items.get(key)
        self._check(ConditionExpression, existing, ExpressionAttributeValues)
        item = dict(existing) if existing else dict(Key)
        names = ExpressionAttributeNames or {}
        values = ExpressionAttributeValues or {}
        assert_storable(values)
        assert len(UpdateExpression.encode()) <= 4096, "UpdateExpression exceeds DynamoDB's 4 KB limit"
        set_part, _, remove_part = UpdateExpression.partition(" REMOVE ")
        if set_part.startswith("ADD "):
            set_part, add_part = "", set_part[4:]
        else:
            set_part, _, add_part = set_part.partition(" ADD ")
        for assignment in set_part.replace("SET ", "", 1).split(", "):
            if not assignment.strip():
                continue
            left, right = [x.strip() for x in assignment.split(" = ")]
            item[names.get(left, left)] = copy.deepcopy(values[right])
        for addition in [a.strip() for a in add_part.split(",") if a.strip()]:
            left, right = addition.split()
            attr = names.get(left, left)
            item[attr] = item.get(attr, 0) + values[right]
        for name in [n.strip() for n in remove_part.split(",") if n.strip()]:
            item.pop(names.get(name, name), None)
        self.items[key] = item
        return {}

    def query(self, KeyConditionExpression: Any, IndexName: str | None = None,
              ExclusiveStartKey: Any = None, Limit: int | None = None, ScanIndexForward: bool = True,
              **_: Any) -> dict[str, Any]:
        self.calls.append(("query", IndexName))
        rows = [copy.deepcopy(i) for i in self.items.values() if _eval(KeyConditionExpression, i)]
        sort_key = f"{IndexName}SK" if IndexName else "SK"
        rows.sort(key=lambda i: str(i.get(sort_key, "")), reverse=not ScanIndexForward)
        return {"Items": rows[:Limit] if Limit else rows}

    def batch_writer(self, **_: Any) -> Any:
        table = self

        class _Writer:
            def __enter__(self) -> "_Writer":
                return self

            def __exit__(self, *exc: Any) -> bool:
                return False

            def delete_item(self, Key: dict[str, Any]) -> None:
                table.delete_item(Key=Key)

            def put_item(self, Item: dict[str, Any]) -> None:
                table.put_item(Item=Item)

        return _Writer()

    # -- convenience for tests ---------------------------------------------
    def doc(self, doc_id: str) -> dict[str, Any] | None:
        return self.items.get((f"DOC#{doc_id}", "META"))

    def keys_with_prefix(self, pk: str) -> list[str]:
        return sorted(sk for (p, sk) in self.items if p == pk)


class FakeResource:
    """``boto3.resource("dynamodb")`` stand-in over one FakeTable."""

    def __init__(self, table: FakeTable, name: str = "test-table") -> None:
        self.table, self.name = table, name
        # More tables by name (the Govern tables — see install_fake_govern).
        self.tables: dict[str, FakeTable] = {name: table}

    def Table(self, name: str) -> FakeTable:  # noqa: N802 — boto3 naming
        return self.tables.get(name, self.table)

    def batch_get_item(self, RequestItems: dict[str, Any]) -> dict[str, Any]:
        responses: dict[str, list[dict[str, Any]]] = {}
        for table_name, spec in RequestItems.items():
            table = self.tables.get(table_name, self.table)
            out = responses.setdefault(table_name, [])
            for key in spec["Keys"]:
                item = table.items.get((key["PK"], key["SK"]))
                if item:
                    out.append(copy.deepcopy(item))
        return {"Responses": responses, "UnprocessedKeys": {}}


def install_fake_dynamodb(monkeypatch: Any) -> FakeTable:
    """Route every DynamoDB access in the code under test to one FakeTable."""
    from shared import aws as shared_aws
    from shared import dynamodb
    from shared.config import settings

    table = FakeTable()
    resource = FakeResource(table)
    monkeypatch.setattr(settings, "table_name", resource.name)
    monkeypatch.setattr(dynamodb, "_table", lambda: table)
    monkeypatch.setattr(dynamodb, "dynamodb_resource", lambda: resource)
    monkeypatch.setattr(shared_aws, "dynamodb_resource", lambda: resource)
    return table


# ---------------------------------------------------------------------------
# Govern: second and third table, EventBridge, SES, Secrets Manager, SQS
# ---------------------------------------------------------------------------


GOVERN_TABLES = ("contracts", "activity", "config", "sync", "metrics")


class GovernFakes:
    """In-memory Govern infrastructure: the five Govern tables (beside the
    documents table) and one object that plays every client the Govern code
    builds (events / sesv2 / secretsmanager / sqs), recording what was sent."""

    def __init__(self, docs: FakeTable, tables: dict[str, FakeTable]) -> None:
        self.docs = docs
        self.tables = tables
        self.contracts, self.activity = tables["contracts"], tables["activity"]
        self.config, self.sync, self.metrics = tables["config"], tables["sync"], tables["metrics"]
        self.events: list[dict[str, Any]] = []
        self.emails: list[dict[str, Any]] = []
        self.secrets: dict[str, str] = {}
        self.sqs_messages: list[dict[str, Any]] = []
        self.fail_events = False

    def put_events(self, Entries: list[dict[str, Any]]) -> dict[str, Any]:
        if self.fail_events:
            return {"FailedEntryCount": len(Entries),
                    "Entries": [{"ErrorCode": "InternalFailure", "ErrorMessage": "x"} for _ in Entries]}
        self.events.extend(copy.deepcopy(Entries))
        return {"FailedEntryCount": 0, "Entries": [{"EventId": str(i)} for i, _ in enumerate(Entries)]}

    def send_email(self, **kwargs: Any) -> dict[str, Any]:
        self.emails.append(kwargs)
        return {"MessageId": f"m{len(self.emails)}"}

    def get_secret_value(self, SecretId: str) -> dict[str, Any]:
        if SecretId not in self.secrets:
            raise ClientError({"Error": {"Code": "ResourceNotFoundException", "Message": "no"}}, "GetSecretValue")
        return {"SecretString": self.secrets[SecretId]}

    def put_secret_value(self, SecretId: str, SecretString: str) -> dict[str, Any]:
        self.secrets[SecretId] = SecretString
        return {}

    def send_message(self, QueueUrl: str, MessageBody: str, **_: Any) -> dict[str, Any]:
        self.sqs_messages.append({"QueueUrl": QueueUrl, "MessageBody": MessageBody})
        return {"MessageId": str(len(self.sqs_messages))}

    # -- convenience for tests ---------------------------------------------
    def contract(self, contract_id: str) -> dict[str, Any] | None:
        return self.contracts.items.get((f"CON#{contract_id}", "META"))

    def activity_for(self, contract_id: str) -> list[dict[str, Any]]:
        rows = [i for (pk, _), i in self.activity.items.items() if pk == f"CON#{contract_id}"]
        return sorted(rows, key=lambda i: i["SK"])

    def actions(self, contract_id: str) -> list[str]:
        return [a["action"] for a in self.activity_for(contract_id)]


def install_fake_govern(monkeypatch: Any, docs: FakeTable) -> GovernFakes:
    """Add the five Govern tables beside the documents table that
    ``install_fake_dynamodb`` installed, and fake EventBridge / SES / Secrets
    Manager / SQS."""
    from shared import aws as shared_aws
    from shared import dynamodb
    from shared.config import settings
    from shared.govern import secrets, store

    resource = dynamodb.dynamodb_resource()
    tables = {name: FakeTable() for name in GOVERN_TABLES}
    for name, table in tables.items():
        resource.tables[f"{name}-table"] = table
        monkeypatch.setattr(settings, f"{name}_table", f"{name}-table")
    monkeypatch.setattr(settings, "event_bus_name", "platform-bus")
    # The dev-stage sandbox: users in no Govern group are admins (tests that
    # need the production default switch it off).
    monkeypatch.setattr(settings, "govern_open_admin", True)
    monkeypatch.setattr(settings, "processed_bucket", "processed")
    monkeypatch.setattr(store, "_resource", lambda: resource)
    fakes = GovernFakes(docs, tables)
    for factory in ("events_client", "ses_client", "secrets_client", "sqs_client"):
        monkeypatch.setattr(shared_aws, factory, lambda _f=fakes: _f, raising=False)
    secrets.clear_cache()
    return fakes


# ---------------------------------------------------------------------------
# OpenAI
# ---------------------------------------------------------------------------


def chat_response(content: str | None, finish_reason: str = "stop", usage: tuple[int, int] | None = None,
                  refusal: str | None = None) -> Any:
    msg = types.SimpleNamespace(content=content, refusal=refusal)
    choice = types.SimpleNamespace(message=msg, finish_reason=finish_reason)
    use = None
    if usage:
        use = types.SimpleNamespace(prompt_tokens=usage[0], completion_tokens=usage[1],
                                    total_tokens=usage[0] + usage[1])
    return types.SimpleNamespace(choices=[choice], usage=use)


class FakeOpenAI:
    """Scripted OpenAI client. ``chat`` and ``embed`` are lists of things to do
    in order: a response object to return, or an exception to raise."""

    def __init__(self, chat: list[Any] | None = None, embed: list[Any] | None = None) -> None:
        self.chat_script = list(chat or [])
        self.embed_script = list(embed or [])
        self.chat_calls: list[dict[str, Any]] = []
        self.embed_calls: list[dict[str, Any]] = []
        self.chat = types.SimpleNamespace(completions=types.SimpleNamespace(create=self._chat))
        self.embeddings = types.SimpleNamespace(create=self._embed)

    def _next(self, script: list[Any]) -> Any:
        step = script.pop(0) if len(script) > 1 else script[0]
        if isinstance(step, BaseException):
            raise step
        return step

    def _chat(self, **kwargs: Any) -> Any:
        self.chat_calls.append(kwargs)
        return self._next(self.chat_script)

    def _embed(self, **kwargs: Any) -> Any:
        self.embed_calls.append(kwargs)
        step = self._next(self.embed_script) if self.embed_script else None
        if step is not None:
            return step
        data = [types.SimpleNamespace(embedding=[float(i)] * 4, index=i) for i, _ in enumerate(kwargs["input"])]
        return types.SimpleNamespace(data=data, usage=None)


class ApiError(Exception):
    """An OpenAI-SDK-shaped error: retry logic keys on the class NAME and status."""

    def __init__(self, message: str = "", status_code: int | None = None, headers: dict[str, str] | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.response = types.SimpleNamespace(headers=headers or {})


class RateLimitError(ApiError):
    pass


class APITimeoutError(ApiError):
    pass


class BadRequestError(ApiError):
    pass
