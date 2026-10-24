import os

# ponytail: plain os.environ; KeyError names the missing var and aborts boot.
ALLOWED_ORIGIN = os.environ["ALLOWED_ORIGIN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
DATABASE_URL = os.environ["DATABASE_URL"]
IP_HASH_SALT = os.environ["IP_HASH_SALT"]
CALENDAR_URL = os.environ["CALENDAR_URL"]
CV_URLS = {"en": os.environ["CV_URL_EN"], "pl": os.environ["CV_URL_PL"]}
GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
GITHUB_HANDOFF_REPO = os.environ["GITHUB_HANDOFF_REPO"]
# Only optional setting: a sane default beats refusing to boot.
DAILY_BUDGET_USD = float(os.environ.get("DAILY_BUDGET_USD", "2"))
