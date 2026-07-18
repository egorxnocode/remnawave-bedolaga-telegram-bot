from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _function_source(relative_path: str, function_name: str, class_name: str | None = None) -> str:
    source = (ROOT / relative_path).read_text(encoding='utf-8')
    tree = ast.parse(source)
    candidates: list[ast.AsyncFunctionDef] = []

    for node in tree.body:
        if class_name is None and isinstance(node, ast.AsyncFunctionDef) and node.name == function_name:
            candidates.append(node)
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            candidates.extend(
                child
                for child in node.body
                if isinstance(child, ast.AsyncFunctionDef) and child.name == function_name
            )

    assert len(candidates) == 1
    return ast.unparse(candidates[0])


def test_every_customer_ticket_write_dispatches_before_commit() -> None:
    entrypoints = (
        ('app/database/crud/ticket.py', 'create_ticket', 'TicketCRUD', 'channel=channel'),
        ('app/database/crud/ticket.py', 'add_message', 'TicketMessageCRUD', 'channel=channel'),
        ('app/cabinet/routes/tickets.py', 'create_ticket', None, "channel='cabinet'"),
        ('app/cabinet/routes/tickets.py', 'add_ticket_message', None, "channel='cabinet'"),
        ('app/cabinet/routes/support_ws.py', '_handle_ticket_reply', None, "channel='support_ws'"),
    )

    for path, function, owner, channel in entrypoints:
        function_source = _function_source(path, function, owner)
        dispatch = function_source.index('ai_support_dispatch_service.on_user_message')
        commit = function_source.index('await db.commit()')
        assert dispatch < commit
        assert channel in function_source


def test_every_human_ticket_write_takes_over_before_commit() -> None:
    entrypoints = (
        ('app/database/crud/ticket.py', 'add_message', 'TicketMessageCRUD'),
        ('app/cabinet/routes/admin_tickets.py', 'reply_to_ticket', None),
        ('app/cabinet/routes/support_ws.py', '_handle_ticket_reply', None),
    )

    for path, function, owner in entrypoints:
        function_source = _function_source(path, function, owner)
        takeover = function_source.index('ai_support_dispatch_service.on_human_reply')
        commit = function_source.index('await db.commit()')
        assert takeover < commit


def test_service_webapi_labels_human_reply_channel() -> None:
    source = _function_source('app/webapi/routes/tickets.py', 'reply_to_ticket')
    assert 'TicketMessageCRUD.add_message' in source
    assert "channel='webapi'" in source
