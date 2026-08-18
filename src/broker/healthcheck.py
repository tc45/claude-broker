"""Docker healthcheck script."""

from __future__ import annotations

import sys
import urllib.error
import urllib.request


def main() -> None:
	try:
		req = urllib.request.Request("http://127.0.0.1:8787/mcp")
		with urllib.request.urlopen(req, timeout=5) as resp:
			if resp.status < 500:
				sys.exit(0)
	except urllib.error.HTTPError as exc:
		if exc.code < 500:
			sys.exit(0)
	except Exception:
		pass
	sys.exit(1)


if __name__ == "__main__":
	main()
