"""Local build tool: writes shippable prototypes into data/workspace + artifacts.
Keeps agent work real even in dry-run (inspectable files, not just API calls)."""
import os

from .base import ToolResult

LANDING_TEMPLATE = """<!doctype html><html><head><meta charset="utf-8">
<title>{name}</title><style>body{{font-family:system-ui;max-width:640px;margin:40px auto;padding:0 16px}}button{{padding:12px 20px;font-size:16px}}</style></head>
<body><h1>{name}</h1><p>{pitch}</p>
<p><strong>{price}</strong></p>
<a href="{checkout}"><button>Buy now</button></a>
<p><small>{proof}</small></p></body></html>"""


class BuildTool:
    name = "build"

    def __init__(self, config, store):
        self.cfg = config
        self.store = store

    def run(self, op, args):
        if op == "build_landing":
            return self.build_landing(args)
        if op == "write_file":
            return self.write_file(args)
        return ToolResult(ok=False, output=f"unknown op {op}")

    def build_landing(self, args):
        name = args.get("name", "auto-product")[:60]
        html = LANDING_TEMPLATE.format(
            name=name,
            pitch=args.get("pitch", "A tiny useful tool. One-time payment, instant access."),
            price=args.get("price", "$9 one-time via Dodo"),
            checkout=args.get("checkout", "#"),
            proof="Sandbox demo build. Wire Dodo checkout for live sales.",
        )
        ws = os.path.join(self.cfg.data_dir, "workspace", name.replace("/", "__"))
        os.makedirs(ws, exist_ok=True)
        with open(os.path.join(ws, "index.html"), "w") as f:
            f.write(html)
        art = os.path.join(self.cfg.data_dir, "artifacts", name)
        os.makedirs(art, exist_ok=True)
        with open(os.path.join(art, "index.html"), "w") as f:
            f.write(html)
        pid = self.store.add_project(args.get("goal_id", 0), name, status="built", url=f"file://{art}/index.html")
        return ToolResult(ok=True, output=f"built landing {name} (project {pid}) -> {art}/index.html",
                          data={"project_id": pid, "path": art})

    def write_file(self, args):
        rel = args.get("path", "notes.txt").lstrip("/").replace("..", "_")
        full = os.path.join(self.cfg.data_dir, "workspace", rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w") as f:
            f.write(args.get("content", ""))
        return ToolResult(ok=True, output=f"wrote {full}")
