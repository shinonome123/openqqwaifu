from __future__ import annotations

import sys
import unittest
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from waifu_standalone.infra.clock import absolutize_relative_dates, format_prompt_time, resolve_timezone


class ClockTests(unittest.TestCase):
    # 2026-10-26 is a Monday.
    TODAY = date(2026, 10, 26)

    def test_day_words_become_dates(self) -> None:
        self.assertEqual(absolutize_relative_dates("阿林明天去杭州", self.TODAY), "阿林2026-10-27去杭州")
        self.assertEqual(absolutize_relative_dates("大后天考试", self.TODAY), "2026-10-29考试")
        self.assertEqual(absolutize_relative_dates("昨晚上没睡好", self.TODAY), "2026-10-25晚上没睡好")

    def test_week_expressions_become_dates(self) -> None:
        self.assertEqual(absolutize_relative_dates("下周三面试", self.TODAY), "2026-11-04（周三）面试")
        self.assertEqual(absolutize_relative_dates("上星期日搬家", self.TODAY), "2026-10-25（周日）搬家")
        self.assertEqual(absolutize_relative_dates("这周末加班", self.TODAY), "2026-10-31那个周末加班")
        self.assertEqual(absolutize_relative_dates("下个礼拜很忙", self.TODAY), "2026-11-02那一周很忙")

    def test_habits_and_lookalikes_are_left_alone(self) -> None:
        for text in ("每周三练琴", "马上周末了", "晚上周三见", "看一下周五的安排", "这周围很安静"):
            self.assertEqual(absolutize_relative_dates(text, self.TODAY), text)

    def test_prompt_time_format(self) -> None:
        moment = datetime(2026, 10, 26, 9, 5, tzinfo=resolve_timezone("Asia/Shanghai"))
        self.assertEqual(format_prompt_time(moment, "Asia/Shanghai"), "2026-10-26 周一 09:05（Asia/Shanghai）")

    def test_unknown_timezone_falls_back_to_utc_plus_8(self) -> None:
        with self.assertLogs("waifu_standalone.infra.clock", level="WARNING"):
            zone = resolve_timezone("Not/AZone")
        self.assertEqual(datetime(2026, 1, 1, tzinfo=zone).utcoffset().total_seconds(), 8 * 3600)


if __name__ == "__main__":
    unittest.main()
