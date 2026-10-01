"""QQ uses real UTC timestamps; durations use a monotonic clock.

The module path is retained for core imports. Legacy clock state is parsed only
by the offline migration tool, never by the running bot.
"""
import datetime
import time
from zoneinfo import ZoneInfo
from config.api_config import config

class RealClock:
    def now(self):
        return time.time()

    def monotonic(self):
        return time.monotonic()

    def to_real_time(self, timestamp):
        return float(timestamp)

    def local_datetime(self, timestamp=None):
        zone = ZoneInfo(config.get("timezone", "Asia/Taipei"))
        return datetime.datetime.fromtimestamp(self.now() if timestamp is None else timestamp, zone)

    def in_sleep_window(self, when=None):
        start = config.get("sleep_start_hour")
        end = config.get("sleep_end_hour")
        if start is None or end is None:
            return False
        if start==end: return False
        hour = (when or self.local_datetime()).hour
        return start <= hour < end if start < end else hour >= start or hour < end

clock = RealClock()
