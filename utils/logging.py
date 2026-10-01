"""Redact configured credentials after formatting, including exception text."""
import logging
import os
from config.api_config import config

def redact(text):
    secrets=[config.get(key) for key in ('primary_api_key','secondary_api_key','napcat_token')]
    secrets.extend(os.environ.get(key) for key in ('HUIYE_NAPCAT_TOKEN','HUIYE_ADMIN_PASSWORD'))
    for secret in secrets:
        if secret and secret!='YOUR_API_KEY_HERE': text=text.replace(str(secret),'[redacted]')
    return text

class SecretFormatter(logging.Formatter):
    def format(self,record):return redact(super().format(record))
