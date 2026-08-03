"""Serve a ChatGPT Plus/Pro subscription as a keyless OpenAI-compatible provider.

  oauth      device-code login, token store, refresh
  translate  chat/completions <-> Responses wire translation (pure)
  server     the aiohttp app
  login      the CLI front end
  acp        launcher for the maintained Codex ACP adapter
"""

__version__ = "1.0.0"
