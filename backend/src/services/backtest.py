"""历史回测：按接收时间还原当时的可得资料，逐时点生成试验。

三条不可违反的口径：
1. 业务发生时间（occurred_at）、标签到期时间（matured_at）与接收时间（received_at）分离；
   received_at 是资料进入系统的唯一时点，接口不可回填或改写。
2. 回测时点只使用 received_at <= 时点 且 matured_at <= 时点 的最新修订；
   时点之后才接收的迟到记录或更正不进入该时点，也不改写既往时点。
3. 建案即冻结接收范围（批次清单 + 接收截止时间）与各时点纳入决定；
   试验按 point_id 唯一，重试复用同一试验，不重复创建。
"""
import hashlib
import json
import math
import time
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import select, func, update, or_, and_

from src.models import HistoryBatch, HistorySample, BacktestCase, BacktestPoint, BacktestTrial
from src.services.business import FEATURES

STALE_RUNNING_SECONDS = 600  # 超过该时长仍处于 running 的时点视为中断，可被恢复执行

REASON_LABELS = {
    'received_after_cutoff': '该时点后才接收',
    'label_not_matured': '标签尚未到期',
    'no_eligible_revision': '此时点无可用修订',
}


# ---------- 时间与指纹 ----------

def parse_moment(value, field='时间'):
    if isinstance(value, (int, float)):
        try:
            moment = float(value)
        except (TypeError, ValueError, OverflowError):
            raise HTTPException(422, f'{field}必须为有效日期或时间戳')
    else:
        text = str(value or '').strip()
        if not text:
            raise HTTPException(422, f'{field}不能为空')
        if text.replace('.', '', 1).isdigit():
            moment = float(text)
        else:
            try:
                parsed = datetime.fromisoformat(text.replace('Z', '+00:00'))
            except ValueError:
                raise HTTPException(422, f'{field}须为 ISO 8601 日期或时间戳，例如 2025-06-30 或 2025-06-30T18:00:00+08:00')
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            moment = parsed.timestamp()
    if not math.isfinite(moment) or moment < 0:
        raise HTTPException(422, f'{field}必须为有效的非负时间')
    return float(moment)


def iso(moment):
    return datetime.fromtimestamp(moment, tz=timezone.utc).isoformat().replace('+00:00', 'Z')


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()


# ---------- 资料接收 ----------

def _validate_row(index, row):
    if not isinstance(row, dict):
        raise HTTPException(422, f'第 {index + 1} 行必须是对象')
    if 'received_at' in row:
        raise HTTPException(422, f'第 {index + 1} 行不可提供 received_at：接收时间由系统在整批接收时写入，不可回填')
    reader_id = str(row.get('reader_id') or '').strip()
    if not reader_id:
        raise HTTPException(422, f'第 {index + 1} 行缺少 reader_id')
    try:
        occurred_at = parse_moment(row.get('occurred_at'), f'第 {index + 1} 行业务发生时间')
    except HTTPException:
        raise HTTPException(422, f'第 {index + 1} 行业务发生时间须为 ISO 8601 日期或时间戳')
    renewed = row.get('renewed')
    if isinstance(renewed, bool) or renewed in (None, ''):
        raise HTTPException(422, f'第 {index + 1} 行续读标签只能为 0/1')
    try:
        renewed = int(float(renewed))
    except (TypeError, ValueError, OverflowError):
        raise HTTPException(422, f'第 {index + 1} 行续读标签只能为 0/1')
    if renewed not in (0, 1):
        raise HTTPException(422, f'第 {index + 1} 行续读标签只能为 0/1')
    values = {}
    for key in FEATURES:
        raw = row.get(key)
        if raw is None or raw == '':
            raise HTTPException(422, f'第 {index + 1} 行缺少行为字段 {key}')
        try:
            number = float(raw)
        except (TypeError, ValueError, OverflowError):
            raise HTTPException(422, f'第 {index + 1} 行 {key} 必须为有限数值')
        if not math.isfinite(number) or number < 0:
            raise HTTPException(422, f'第 {index + 1} 行 {key} 必须为非负有限数值')
        if key == 'completion_pct' and number > 100:
            raise HTTPException(422, f'第 {index + 1} 行 completion_pct 须在 0~100')
        values[key] = number
    try:
        revision = int(float(row.get('revision')))
    except (TypeError, ValueError, OverflowError):
        raise HTTPException(422, f'第 {index + 1} 行 revision 必须为正整数')
    if revision < 1:
        raise HTTPException(422, f'第 {index + 1} 行 revision 必须为正整数')
    try:
        matured_at = parse_moment(row.get('matured_at'), f'第 {index + 1} 行标签到期时间')
    except HTTPException:
        raise HTTPException(422, f'第 {index + 1} 行标签到期时间须为 ISO 8601 日期或时间戳')
    if matured_at < occurred_at:
        raise HTTPException(422, f'第 {index + 1} 行标签到期时间不能早于业务发生时间')
    return {
        'reader_id': reader_id, 'revision': revision, 'renewed': renewed,
        'occurred_at': occurred_at, 'matured_at': matured_at, **values,
    }


def receive_batch(db, data, user, now=None):
    """整批接收：先校验全部行，再原子写入本批样本并盖接收时间。

    接收时间由服务端在整批校验通过后统一写入；并发接收的多批各自成批，
    不会共享半批资料，也不会彼此混入。
    """
    received_at = time.time() if now is None else float(now)
    name = data.name.strip()
    if db.scalar(select(HistoryBatch).where(HistoryBatch.name == name)):
        raise HTTPException(409, '该历史资料批次名已存在，请修改名称')
    rows = [_validate_row(i, row) for i, row in enumerate(data.records)]
    seen = set()
    for row in rows:
        key = (row['reader_id'], row['revision'])
        if key in seen:
            raise HTTPException(422, f'批次内存在重复修订：{row["reader_id"]} 第 {row["revision"]} 次')
        seen.add(key)
        existing = db.scalar(select(HistorySample).where(
            HistorySample.reader_id == row['reader_id'], HistorySample.revision == row['revision']))
        if existing:
            raise HTTPException(409, f'{row["reader_id"]} 第 {row["revision"]} 次修订已接收，历史修订不可覆盖')
    batch = HistoryBatch(name=name, source=data.source.strip() or 'history.json', note=data.note,
                         status='received', record_count=len(rows),
                         checksum=_hash(rows), received_at=received_at,
                         created_by=user.id, created_at=time.time())
    db.add(batch)
    db.flush()
    for row in rows:
        db.add(HistorySample(batch_id=batch.id, reader_id=row['reader_id'], revision=row['revision'],
                             sessions_14d=row['sessions_14d'], completion_pct=row['completion_pct'],
                             days_inactive=row['days_inactive'], issues_read=row['issues_read'],
                             renewed=row['renewed'], occurred_at=row['occurred_at'],
                             matured_at=row['matured_at'], received_at=received_at,
                             created_at=time.time()))
    return batch


# ---------- 建案 ----------

def _latest_revision_by_reader(samples):
    latest = {}
    for sample in samples:
        current = latest.get(sample.reader_id)
        if current is None or sample.revision > current.revision:
            latest[sample.reader_id] = sample
    return latest


def _point_snapshot(samples, cutoff):
    """仅用 cutoff 前已接收且标签已到期的最新修订；迟到与更正不改写过去。"""
    eligible = [s for s in samples if s.received_at <= cutoff and s.matured_at <= cutoff]
    latest = _latest_revision_by_reader(eligible)
    decisions = []
    for reader_id in sorted(latest):
        sample = latest[reader_id]
        decisions.append({'reader_id': reader_id, 'revision': sample.revision, 'renewed': sample.renewed,
                          'received_at': sample.received_at, 'matured_at': sample.matured_at,
                          'occurred_at': sample.occurred_at})
    snapshot = [{'reader_id': sample.reader_id, 'revision': sample.revision, 'renewed': sample.renewed,
                 'occurred_at': sample.occurred_at, 'matured_at': sample.matured_at,
                 'received_at': sample.received_at,
                 **{key: getattr(sample, key) for key in FEATURES}}
                for _, sample in sorted(latest.items())]
    return snapshot, decisions


def _exclusion_summary(batch_samples, latest_eligible, cutoff):
    """记录读者层面的排除原因，供页面逐点核对。"""
    reasons = {}
    excluded = []
    eligible_ids = set(latest_eligible)
    for reader_id in sorted({s.reader_id for s in batch_samples}):
        if reader_id in eligible_ids:
            continue
        revisions = [s for s in batch_samples if s.reader_id == reader_id]
        received = [s for s in revisions if s.received_at <= cutoff]
        after = [s for s in revisions if s.received_at > cutoff]
        unmatured = [s for s in received if s.matured_at > cutoff]
        if unmatured:
            reason = 'label_not_matured'
            detail = {'earliest_matured_at': iso(min(s.matured_at for s in unmatured))}
        elif after:
            reason = 'received_after_cutoff'
            detail = {'latest_received_at': iso(max(s.received_at for s in after))}
        else:
            reason = 'no_eligible_revision'
            detail = {}
        reasons[reason] = reasons.get(reason, 0) + 1
        excluded.append({'reader_id': reader_id, 'reason': reason,
                         'reason_label': REASON_LABELS[reason], **detail})
    return reasons, excluded


def plan_case(db, data, user, now=None):
    receive_cutoff = parse_moment(data.receive_cutoff, '接收截止时间')
    now = time.time() if now is None else float(now)
    if receive_cutoff > now:
        raise HTTPException(422, '接收截止时间不能晚于当前时间；冻结范围只包含已接收资料')
    name = data.name.strip()
    if db.scalar(select(BacktestCase).where(BacktestCase.name == name)):
        raise HTTPException(409, '该回测案名已存在，请修改名称')
    params = dict(data.params or {})
    unknown = set(params) - {'threshold', 'regularization'}
    if unknown:
        raise HTTPException(422, f'包含未知分析参数：{sorted(unknown)}')
    try:
        threshold = float(params.get('threshold', 0.5))
        regularization = float(params.get('regularization', 1.0))
    except (TypeError, ValueError, OverflowError):
        raise HTTPException(422, '风险阈值与模型C须为有效数值')
    if not 0.1 <= threshold <= 0.9 or not 0.01 <= regularization <= 10:
        raise HTTPException(422, '风险阈值须为0.1~0.9，模型C须为0.01~10')
    params = {'threshold': threshold, 'regularization': regularization}
    points = sorted({parse_moment(p, '回测时点') for p in data.points})
    if any(p > receive_cutoff for p in points):
        raise HTTPException(422, '回测时点不能晚于接收截止时间')
    if any(p > now for p in points):
        raise HTTPException(422, '回测时点不能晚于当前时间；未来时点没有可回测的资料')
    if len(points) < 1:
        raise HTTPException(422, '至少需要一个回测时点')

    batches = list(db.scalars(select(HistoryBatch).where(
        HistoryBatch.status == 'received', HistoryBatch.received_at <= receive_cutoff).order_by(HistoryBatch.id)))
    if data.batch_ids is not None:
        wanted = set(data.batch_ids)
        known = {b.id: b for b in batches}
        missing = sorted(wanted - set(known))
        if missing:
            raise HTTPException(422, f'批次 {missing} 不在接收截止前已完成接收的范围内')
        batches = [b for b in batches if b.id in wanted]
    if not batches:
        raise HTTPException(422, '接收截止前没有已完成接收的历史资料，无法冻结回测范围')

    samples = list(db.scalars(select(HistorySample).where(
        HistorySample.batch_id.in_([b.id for b in batches]))))
    scope_snapshot = [{'id': b.id, 'name': b.name, 'source': b.source, 'record_count': b.record_count,
                       'checksum': b.checksum, 'received_at': b.received_at, 'received_at_iso': iso(b.received_at)}
                      for b in batches]

    case = BacktestCase(name=name, status='queued', receive_cutoff=receive_cutoff,
                        scope_batch_ids=[b.id for b in batches], scope_snapshot=scope_snapshot,
                        scope_checksum=_hash(scope_snapshot), points_count=len(points),
                        params=params, created_by=user.id, created_at=time.time())
    db.add(case)
    db.flush()

    point_rows = []
    unrunnable = []
    for sequence, cutoff in enumerate(points, start=1):
        snapshot, decisions = _point_snapshot(samples, cutoff)
        try:
            _check_point_runnable(snapshot, params)
        except ValueError as exc:
            unrunnable.append(f'第 {sequence} 个时点（{iso(cutoff)}）：{exc}')
            continue
        eligible = {d['reader_id'] for d in decisions}
        reasons, excluded = _exclusion_summary(samples, eligible, cutoff)
        included_count = len(snapshot)
        point = BacktestPoint(case_id=case.id, sequence=sequence, cutoff_at=cutoff, status='queued',
                              snapshot=snapshot, decisions=decisions, reason_counts=reasons,
                              exclusions=excluded,
                              included_count=included_count,
                              excluded_count=len({s.reader_id for s in samples}) - included_count,
                              snapshot_checksum=_hash(snapshot), attempts=0, error='')
        db.add(point)
        db.flush()
        point_rows.append(point)
    if unrunnable:
        # 冻结范围内根本无法计算的时点应在建案时拒绝，避免案子永远无法齐全。
        db.rollback()
        raise HTTPException(422, '以下时点在冻结范围内样本不足，无法形成试验，请调整时点或补充接收资料：'
                                 + '；'.join(unrunnable))
    return case, point_rows


def _check_point_runnable(snapshot, params):
    from src.services.business import validate_inputs
    validate_inputs(snapshot, params)


# ---------- 逐点执行 ----------

def _trial_params(case, point):
    params = dict(case.params or {})
    return {'threshold': float(params.get('threshold', 0.5)),
            'regularization': float(params.get('regularization', 1.0))}


def run_point(db, case, point):
    """幂等执行一个时点：已成功的试验直接复用，失败则重试同一试验。

    先用条件更新抢占时点，确保同一时点只被一个执行者处理；
    已完成的时点不会被重复计算，已存在的试验也不会被重新创建。
    """
    if point.status == 'completed':
        return 'skipped'
    stale_before = time.time() - STALE_RUNNING_SECONDS
    # 可抢占：未完成的排队时点，或长时间停留在 running 的中断时点。
    claimable = or_(BacktestPoint.status == 'queued', BacktestPoint.status == 'failed',
                    and_(BacktestPoint.status == 'running',
                         or_(BacktestPoint.started_at.is_(None), BacktestPoint.started_at < stale_before)))
    claimed = db.execute(update(BacktestPoint)
                         .where(BacktestPoint.id == point.id, BacktestPoint.status != 'completed',
                                claimable)
                         .values(status='running', attempts=BacktestPoint.attempts + 1,
                                 started_at=time.time(), error='')).rowcount
    db.commit()
    db.refresh(point)
    if not claimed:
        return 'skipped'
    trial = db.scalar(select(BacktestTrial).where(BacktestTrial.point_id == point.id))
    if trial and trial.status == 'completed':
        point.status = 'completed'
        point.error = ''
        db.commit()
        return 'skipped'
    if trial is None:
        trial = BacktestTrial(case_id=case.id, point_id=point.id, status='pending',
                              params=_trial_params(case, point), result={}, error='',
                              attempts=0, created_at=time.time())
        db.add(trial)
        try:
            db.flush()
        except Exception:
            # 并发执行时由唯一约束兜底：复用已存在的同一试验。
            db.rollback()
            trial = db.scalar(select(BacktestTrial).where(BacktestTrial.point_id == point.id))
            if trial is None:
                raise
    if trial.status == 'completed':
        point.status = 'completed'
        point.error = ''
        db.commit()
        return 'skipped'
    trial.attempts = int(trial.attempts or 0) + 1
    trial.status = 'running'
    trial.started_at = trial.started_at or time.time()
    db.commit()
    try:
        from src.services.business import analyze
        rows = point.snapshot or []
        result = analyze(rows, trial.params)
        result['point'] = point_metrics(point, trial.params)
        result['receipt_cutoff_note'] = f'仅使用接收时间≤{iso(point.cutoff_at)} 且标签到期时间≤该时点、修订号最新的样本' 
        json.dumps(result, allow_nan=False)
        trial.result = result
        trial.status = 'completed'
        trial.error = ''
        trial.finished_at = time.time()
        point.status = 'completed'
        point.error = ''
        point.finished_at = time.time()
    except Exception as exc:  # 单点失败保留已完成的其他时点，可恢复重试
        trial.status = 'failed'
        trial.error = str(exc)[:990]
        trial.finished_at = time.time()
        point.status = 'failed'
        point.error = trial.error
        point.finished_at = time.time()
    db.commit()
    return 'completed' if point.status == 'completed' else 'failed'


def refresh_case_status(db, case):
    statuses = list(db.scalars(select(BacktestPoint.status).where(BacktestPoint.case_id == case.id)))
    if any(s == 'running' for s in statuses):
        # 并发执行者仍在跑；不要把案状态误判为部分完成。
        if case.status != 'completed':
            case.status = 'running'
        return case.status
    if statuses and all(s == 'completed' for s in statuses):
        if case.status != 'completed':
            case.status = 'completed'
            case.finished_at = time.time()
            case.last_resumed_at = time.time()
    elif any(s in ('running', 'queued') for s in statuses):
        case.status = 'running'
    elif any(s == 'failed' for s in statuses):
        case.status = 'partial'
    return case.status


def run_case(db, case):
    points = list(db.scalars(select(BacktestPoint).where(BacktestPoint.case_id == case.id)
                             .order_by(BacktestPoint.sequence)))
    remaining = [p for p in points if p.status != 'completed']
    if not remaining:
        if case.status != 'completed':
            refresh_case_status(db, case)
            db.commit()
        return case
    case.started_at = case.started_at or time.time()
    case.last_resumed_at = time.time()
    case.status = 'running'
    case.finished_at = None
    db.commit()
    for point in remaining:
        # 每个时点重新读取状态：同一时点可能已被并发执行者完成。
        db.refresh(point)
        if point.status == 'completed':
            continue
        run_point(db, case, point)
    refresh_case_status(db, case)
    db.commit()
    return case


def resume_case(db, case):
    """中断后按原进度恢复：只把中断的时点放回队列，不重建已完成的试验。

    实际执行由 worker 继续，请求立即返回当前进度，页面可轮询核对。
    """
    if case.approved:
        raise HTTPException(409, '回测案已获批准，不能再重跑时点')
    pending = [p for p in db.scalars(select(BacktestPoint).where(BacktestPoint.case_id == case.id))
               if p.status != 'completed']
    if not pending and case.status == 'completed':
        raise HTTPException(409, '该回测案各时点均已成功，无需恢复')
    # 中断留下的 running 状态回到 queued；已成功的时点和已保存结果保持不变。
    db.execute(update(BacktestPoint).where(BacktestPoint.case_id == case.id,
                                           BacktestPoint.status == 'running').values(status='queued'))
    db.execute(update(BacktestTrial).where(BacktestTrial.case_id == case.id,
                                           BacktestTrial.status == 'running').values(status='pending'))
    case.last_resumed_at = time.time()
    if case.status == 'completed':
        case.status = 'running'
    elif case.status == 'partial':
        case.status = 'queued'
    db.commit()
    return case


# ---------- 门禁与输出 ----------

def completeness(db, case):
    total = db.scalar(select(func.count()).select_from(BacktestPoint).where(BacktestPoint.case_id == case.id)) or 0
    completed = db.scalar(select(func.count()).select_from(BacktestPoint).where(
        BacktestPoint.case_id == case.id, BacktestPoint.status == 'completed')) or 0
    failed = db.scalar(select(func.count()).select_from(BacktestPoint).where(
        BacktestPoint.case_id == case.id, BacktestPoint.status == 'failed')) or 0
    pending = total - completed - failed
    return {'points_total': total, 'points_completed': completed, 'points_failed': failed,
            'points_pending': pending, 'all_completed': total > 0 and completed == total,
            'downloadable': total > 0 and completed == total}


def require_complete(db, case):
    state = completeness(db, case)
    if not state['all_completed']:
        raise HTTPException(409, f'回测案尚未齐全：{state["points_completed"]}/{state["points_total"]} 个时点完成，'
                                 f'{state["points_failed"]} 个失败、{state["points_pending"]} 个未完成；齐全前不开放下载、批准或简报')
    return state


def series_summary(db, case):
    """跨时点趋势：仅汇总各时点自身状态，并在响应中声明。"""
    points = list(db.scalars(select(BacktestPoint).where(BacktestPoint.case_id == case.id).order_by(BacktestPoint.sequence)))
    trials = {t.point_id: t for t in db.scalars(select(BacktestTrial).where(BacktestTrial.case_id == case.id))}
    series = []
    for point in points:
        trial = trials.get(point.id)
        metrics = (trial.result or {}).get('point') if trial and trial.status == 'completed' else None
        series.append({'sequence': point.sequence, 'cutoff_iso': iso(point.cutoff_at), 'status': point.status,
                       'included': point.included_count, 'excluded': point.excluded_count,
                       'reason_counts': point.reason_counts, 'metrics': metrics})
    return {'case_id': case.id, 'points': series,
            'note': '仅已完成的时点有指标；未完成时点不参与任何汇总。'}


def build_export(db, case):
    require_complete(db, case)
    points = list(db.scalars(select(BacktestPoint).where(BacktestPoint.case_id == case.id).order_by(BacktestPoint.sequence)))
    trials = {t.point_id: t for t in db.scalars(select(BacktestTrial).where(BacktestTrial.case_id == case.id))}
    payload = {
        'case': case_dict(db, case, detail=False),
        'scope': case.scope_snapshot,
        'scope_checksum': case.scope_checksum,
        'receipt_policy': '每时点仅使用 received_at 与 matured_at 均不晚于时点的最新修订；迟到记录与更正不改写过去。',
        'points': [point_dict(p, trials.get(p.id), include_snapshot=True) for p in points],
        'series': series_summary(db, case)['points'],
    }
    return payload


def case_dict(db, case, detail=True):
    state = completeness(db, case)
    body = {'id': case.id, 'name': case.name, 'status': case.status,
            'receive_cutoff': case.receive_cutoff, 'receive_cutoff_iso': iso(case.receive_cutoff),
            'scope_batch_ids': case.scope_batch_ids, 'scope_snapshot': case.scope_snapshot,
            'scope_checksum': case.scope_checksum, 'points_count': case.points_count,
            'params': case.params, 'created_at': case.created_at, 'started_at': case.started_at,
            'finished_at': case.finished_at, 'last_resumed_at': case.last_resumed_at,
            'approved': case.approved, 'approved_by': case.approved_by, 'approved_at': case.approved_at,
            'completeness': state}
    if detail:
        points = list(db.scalars(select(BacktestPoint).where(BacktestPoint.case_id == case.id).order_by(BacktestPoint.sequence)))
        trials = {t.point_id: t for t in db.scalars(select(BacktestTrial).where(BacktestTrial.case_id == case.id))}
        body['points'] = [point_dict(p, trials.get(p.id), include_snapshot=False) for p in points]
    return body


def point_dict(point, trial, include_snapshot=False):
    body = {'id': point.id, 'sequence': point.sequence, 'cutoff_at': point.cutoff_at,
            'cutoff_iso': iso(point.cutoff_at), 'status': point.status,
            'included_count': point.included_count, 'excluded_count': point.excluded_count,
            'reason_counts': point.reason_counts, 'reason_labels': REASON_LABELS,
            'snapshot_checksum': point.snapshot_checksum, 'attempts': point.attempts,
            'error': point.error, 'started_at': point.started_at, 'finished_at': point.finished_at,
            'exclusions': point.exclusions or [],
            'trial': None if trial is None else {
                'id': trial.id, 'status': trial.status, 'attempts': trial.attempts, 'params': trial.params,
                'error': trial.error, 'result': trial.result,
                'started_at': trial.started_at, 'finished_at': trial.finished_at}}
    if include_snapshot:
        body['snapshot'] = point.snapshot
        body['decisions'] = point.decisions
    return body




# ---------- 队列 ----------

def enqueue_case(case_id):
    from redis import Redis
    from src.config.settings import REDIS_URL
    try:
        client = Redis.from_url(REDIS_URL)
        client.rpush('backtest:queue', case_id)
        client.close()
    except Exception:
        pass  # 数据库中的 queued 状态是持久兜底


# ---------- 历史回测指标 ----------

def point_metrics(point, trial_params):
    """一个时点的样本内判别与校准指标，供逐点试验结果使用。

    这里明确标注为同批样本内评估：历史回测只回答“当时按该口径会得到什么排序与覆盖”，
    不是时间外验证，也不替代上线后的随机干预实验。
    """
    from src.services.business import validate_inputs
    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import roc_auc_score, brier_score_loss
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler
    import numpy as np

    rows = point.snapshot or []
    threshold, c_value, x, y = validate_inputs(rows, trial_params)
    model = make_pipeline(StandardScaler(), LogisticRegression(C=c_value, max_iter=1000, random_state=42))
    model.fit(x, y)
    probability = model.predict_proba(x)[:, 1]
    decision = (probability >= threshold).astype(int)
    positives = int(y.sum())
    return {
        'included': len(rows),
        'positives': positives,
        'negatives': len(rows) - positives,
        'threshold': threshold,
        'regularization': c_value,
        'auc': round(float(roc_auc_score(y, probability)), 4),
        'brier': round(float(brier_score_loss(y, probability)), 4),
        'flagged': int(decision.sum()),
        'coverage_pct': round(float(decision.mean()) * 100, 2),
        'precision': round(float((decision & y).sum() / decision.sum()) * 100, 2) if decision.sum() else None,
        'recall': round(float((decision & y).sum() / positives) * 100, 2) if positives else None,
        'basis': '同一时点样本内重代入评估（非时间外验证）',
    }
