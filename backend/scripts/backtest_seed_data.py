"""历史回测演示数据：按接收批次写入，包含迟到资料与事后更正。

生成口径：
- 业务发生时间（occurred_at）为 2025 年各月月末，标签到期时间（matured_at）为发生日 +14 天，
  接收时间（received_at）为资料整批到达系统的时间，由接收批次决定。
- 常规批次在标签到期后次月 20 日接收。
- 更正批次在 2025-10-20 接收，对 2025-06 的部分读者给出第 2 次修订；时点早于接收时间时仍用旧修订。
- 迟到批次在 2026-03-10 接收，包含 2025 年发生的记录；不应进入任何 2025 年时点。
- 补齐批次在 2026-11-10 接收，包含 2025 年发生的记录；属于今天补齐的资料，不参与去年回测。
"""
import math
import time
from datetime import datetime, timezone

import numpy as np


def _moment(year, month, day, hour=12):
    return datetime(year, month, day, hour, tzinfo=timezone.utc).timestamp()


def _features(rng, reader_index, cohort):
    sessions = int(rng.poisson(5.5))
    completion = float(np.clip(rng.normal(60, 20), 4, 99))
    inactive = int(rng.integers(0, 15))
    issues = int(rng.integers(1, 25))
    logit = -0.4 - 0.22 * sessions - 0.024 * (completion - 50) + 0.28 * inactive - 0.03 * issues
    risk = 1 / (1 + math.exp(-logit))
    renewed = int(rng.random() >= risk)
    return {'reader_id': f'HR-{cohort:02d}-{reader_index:04}', 'revision': 1,
            'sessions_14d': sessions, 'completion_pct': round(completion, 1),
            'days_inactive': inactive, 'issues_read': issues, 'renewed': renewed,
            'cohort': cohort}


def generate():
    """返回 (batches, samples)。batches 按接收批次组织，samples 保留接收与业务时间。"""
    rng = np.random.default_rng(20251116)
    cohorts = []
    readers = 200
    for month in range(1, 11):
        cohort = []
        for index in range(1, readers + 1):
            row = _features(rng, index, month)
            occurred = _moment(2025, month, 28) - (index % 3) * 86400
            row['occurred_at'] = occurred
            row['matured_at'] = occurred + 14 * 86400
            cohort.append(row)
        cohorts.append((month, cohort))

    batches = []
    for month, cohort in cohorts:
        receive_year, receive_month = (2026, 1) if month == 12 else (2025, month + 1)
        receive = _moment(receive_year, receive_month, 20, 9)
        batches.append({'name': f'2025-{month:02d} 续读回执批 · {receive_month:02d}月接收',
                        'source': f'reader_renewal_2025_{month:02d}.csv',
                        'note': f'{month} 月到期读者的 14 日行为窗口与续读结果',
                        'received_at': receive, 'rows': cohort})

    # 更正：6 月队列的 40 位读者在 2025-10-20 收到第 2 次修订，不改变业务发生与标签到期时间。
    corrections = []
    for row in cohorts[5][1][:40]:
        corrected = dict(row)
        corrected['revision'] = 2
        corrected['completion_pct'] = round(min(99, row['completion_pct'] + 11), 1)
        corrected['sessions_14d'] = row['sessions_14d'] + 3
        corrections.append(corrected)
    batches.append({'name': '2025-06 队列更正批 · 10月接收',
                    'source': 'reader_renewal_2025_06_correction.csv',
                    'note': '覆盖完成率与访问次数的登记更正；仅接收时间之后生效',
                    'received_at': _moment(2025, 10, 20, 9), 'rows': corrections})

    # 迟到：2025-04 发生、2026-03 才送达的 30 位读者。
    late = []
    for offset in range(30):
        row = _features(rng, 900 + offset, 14)
        occurred = _moment(2025, 4, 26) - offset * 3600
        row['occurred_at'] = occurred
        row['matured_at'] = occurred + 14 * 86400
        late.append(row)
    batches.append({'name': '2025-04 迟到补给批 · 次年3月接收',
                    'source': 'reader_renewal_2025_04_late.csv',
                    'note': '业务发生在 2025-04，实际接收在 2026-03；不得回填 2025 年时点',
                    'received_at': _moment(2026, 3, 10, 9), 'rows': late})

    # 今天补齐：2025-08 发生的 25 位读者，2026-11-10 才收到，不能进入去年回测。
    supplement = []
    for offset in range(25):
        row = _features(rng, 950 + offset, 15)
        occurred = _moment(2025, 8, 27) - offset * 3600
        row['occurred_at'] = occurred
        row['matured_at'] = occurred + 14 * 86400
        supplement.append(row)
    batches.append({'name': '2025-08 补齐资料 · 今日接收',
                    'source': 'reader_renewal_2025_08_supplement.csv',
                    'note': '今天补齐的去年资料；接收时间不可回填，只影响今后建立的回测案',
                    'received_at': time.time() - 3600, 'rows': supplement})
    return batches


if __name__ == '__main__':
    for batch in generate():
        print(batch['name'], len(batch['rows']), batch['received_at'])
