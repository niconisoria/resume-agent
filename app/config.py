import os

# ponytail: plain os.environ; KeyError names the missing var and aborts boot.
ALLOWED_ORIGIN = os.environ["ALLOWED_ORIGIN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
DATABASE_URL = os.environ["DATABASE_URL"]
IP_HASH_SALT = os.environ["IP_HASH_SALT"]
