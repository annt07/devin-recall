"""Builder for a fake Devin sessions.db (test-only; never touches the real one)."""
import json
import sqlite3
from pathlib import Path


def make_db(path: Path, rows: list[dict], with_chat_message: bool = True) -> Path:
    """rows: {"id", "cwd", "created", "last", "title", "workspace_dirs", "messages": [(role, content)]}.

    Timestamps are ms. Content can be a string or a list of {"text": ...}.
    """
    path = Path(path)
    con = sqlite3.connect(path)
    try:
        con.execute("CREATE TABLE sessions(id TEXT PRIMARY KEY, working_directory TEXT, created_at INTEGER,"
                    " last_activity_at INTEGER, title TEXT, workspace_dirs TEXT)")
        msg_col = "chat_message TEXT" if with_chat_message else "payload TEXT"
        con.execute("CREATE TABLE message_nodes(row_id INTEGER PRIMARY KEY, session_id TEXT, node_id TEXT,"
                    f" parent_node_id TEXT, {msg_col}, created_at INTEGER)")
        for r in rows:
            con.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?)",
                        (r["id"], r["cwd"], r["created"], r.get("last", r["created"]), r.get("title", ""),
                         r.get("workspace_dirs")))
            parent = None
            for i, (role, content) in enumerate(r.get("messages", [])):
                node = f"{r['id']}-{i}"
                con.execute("INSERT INTO message_nodes(session_id, node_id, parent_node_id, "
                            f"{'chat_message' if with_chat_message else 'payload'}, created_at) VALUES (?,?,?,?,?)",
                            (r["id"], node, parent, json.dumps({"role": role, "content": content}),
                             r["created"] + i))
                parent = node
        con.commit()
    finally:
        con.close()
    return path
