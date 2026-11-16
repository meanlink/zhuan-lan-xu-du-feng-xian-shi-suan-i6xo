import csv
import io
import json
import os
import hashlib
import time

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, Response
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from src.config.database import get_db
from src.config.settings import ARTIFACT_DIR
from src.middlewares.auth import current_user, can_write, admin_only
from src.models import HistoryBatch, HistorySample, BacktestCase, BacktestBriefing
from src.schemas.backtest import HistoryBatchInput, BacktestCaseInput, BacktestBriefingInput
from src.services import backtest
from src.services.audit import record

router = APIRouter(prefix='/backtest')


def _batch_dict(batch):
    return {'id': batch.id, 'name': batch.name, 'source': batch.source, 'note': batch.note,
            'status': batch.status, 'record_count': batch.record_count, 'checksum': batch.checksum,
            'received_at': batch.received_at, 'received_at_iso': backtest.iso(batch.received_at),
            'created_by': batch.created_by, 'created_at': batch.created_at}


def _briefing_dict(briefing):
    return {'id': briefing.id, 'case_id': briefing.case_id, 'name': briefing.name,
            'destination': briefing.destination, 'filename': briefing.filename,
            'checksum': briefing.checksum, 'created_by': briefing.created_by,
            'created_at': briefing.created_at}


@router.post('/batches', status_code=201)
def receive_batch(data: HistoryBatchInput, db=Depends(get_db), user=Depends(can_write)):
    batch = backtest.receive_batch(db, data, user)
    record(db, user.email, 'history.received', {'batch': batch.id, 'rows': batch.record_count,
                                                'received_at': batch.received_at})
    try:
        db.commit()
    except IntegrityError:
        # 并发写入同名批次或同一修订时整批回滚，不留下半批资料。
        db.rollback()
        raise HTTPException(409, '该批次或其中修订已被并发接收，请刷新后重试')
    return _batch_dict(batch)


@router.get('/batches')
def list_batches(db=Depends(get_db), user=Depends(current_user)):
    batches = list(db.scalars(select(HistoryBatch).order_by(HistoryBatch.id.desc())))
    return [_batch_dict(b) for b in batches]


@router.get('/batches/{id}')
def batch_detail(id: int, db=Depends(get_db), user=Depends(current_user)):
    batch = db.get(HistoryBatch, id)
    if not batch:
        raise HTTPException(404, '历史资料批次不存在')
    samples = list(db.scalars(select(HistorySample).where(HistorySample.batch_id == id)
                              .order_by(HistorySample.reader_id, HistorySample.revision)))
    rows = [{key: getattr(s, key) for key in
             ['reader_id', 'revision', 'sessions_14d', 'completion_pct', 'days_inactive', 'issues_read', 'renewed']}
            | {'occurred_at': s.occurred_at, 'matured_at': s.matured_at, 'received_at': s.received_at}
            for s in samples]
    return {**_batch_dict(batch), 'samples': rows,
            'receipt_policy': '接收时间由服务端在整批接收时写入，不接受回填；同一读者的新修订以新批次接收，不改写旧修订。'}


@router.get('/revisions/{reader_id}')
def revision_history(reader_id: str, db=Depends(get_db), user=Depends(current_user)):
    samples = list(db.scalars(select(HistorySample).where(HistorySample.reader_id == reader_id)
                              .order_by(HistorySample.revision)))
    if not samples:
        raise HTTPException(404, '未找到该读者的历史修订')
    return {'reader_id': reader_id,
            'revisions': [{'revision': s.revision, 'batch_id': s.batch_id, 'renewed': s.renewed,
                           'occurred_at': s.occurred_at, 'matured_at': s.matured_at,
                           'received_at': s.received_at, 'received_at_iso': backtest.iso(s.received_at)}
                          for s in samples]}


@router.post('/cases', status_code=201)
def create_case(data: BacktestCaseInput, db=Depends(get_db), user=Depends(can_write)):
    case, points = backtest.plan_case(db, data, user)
    record(db, user.email, 'backtest.case.create', {'case': case.id, 'points': case.points_count,
                                                    'scope_checksum': case.scope_checksum,
                                                    'receive_cutoff': case.receive_cutoff})
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, '该回测案已被并发建立，请刷新后核对')
    return backtest.case_dict(db, case)


@router.get('/cases')
def list_cases(db=Depends(get_db), user=Depends(current_user)):
    return [backtest.case_dict(db, c, detail=False)
            for c in db.scalars(select(BacktestCase).order_by(BacktestCase.id.desc()))]


@router.get('/cases/{id}')
def case_detail(id: int, db=Depends(get_db), user=Depends(current_user)):
    case = db.get(BacktestCase, id)
    if not case:
        raise HTTPException(404, '回测案不存在')
    return backtest.case_dict(db, case)


@router.post('/cases/{id}/run')
def run_case(id: int, db=Depends(get_db), user=Depends(can_write)):
    case = db.get(BacktestCase, id)
    if not case:
        raise HTTPException(404, '回测案不存在')
    if case.approved:
        raise HTTPException(409, '回测案已获批准，不能再重跑时点')
    if case.status == 'completed':
        raise HTTPException(409, '该回测案各时点均已成功，无需重复执行')
    record(db, user.email, 'backtest.case.run', {'case': id, 'from': case.status})
    db.commit()
    backtest.enqueue_case(case.id)
    return backtest.case_dict(db, case)


@router.post('/cases/{id}/resume')
def resume_case(id: int, db=Depends(get_db), user=Depends(can_write)):
    case = db.get(BacktestCase, id)
    if not case:
        raise HTTPException(404, '回测案不存在')
    backtest.resume_case(db, case)
    record(db, user.email, 'backtest.case.resume', {'case': id, 'status': case.status})
    db.commit()
    backtest.enqueue_case(case.id)
    return backtest.case_dict(db, case)


@router.post('/cases/{id}/approve')
def approve_case(id: int, db=Depends(get_db), user=Depends(admin_only)):
    case = db.get(BacktestCase, id)
    if not case:
        raise HTTPException(404, '回测案不存在')
    backtest.require_complete(db, case)
    case.approved = True
    case.approved_by = user.email
    case.approved_at = time.time()
    record(db, user.email, 'backtest.case.approve', {'case': id,
                                                     'scope_checksum': case.scope_checksum})
    db.commit()
    return backtest.case_dict(db, case)


@router.get('/cases/{id}/series')
def case_series(id: int, db=Depends(get_db), user=Depends(current_user)):
    case = db.get(BacktestCase, id)
    if not case:
        raise HTTPException(404, '回测案不存在')
    return backtest.series_summary(db, case)


@router.get('/cases/{id}/points/{point_id}')
def point_detail(id: int, point_id: int, db=Depends(get_db), user=Depends(current_user)):
    case = db.get(BacktestCase, id)
    if not case:
        raise HTTPException(404, '回测案不存在')
    point = db.scalar(select(backtest.BacktestPoint).where(
        backtest.BacktestPoint.case_id == id, backtest.BacktestPoint.id == point_id))
    if not point:
        raise HTTPException(404, '回测时点不存在')
    trial = db.scalar(select(backtest.BacktestTrial).where(backtest.BacktestTrial.point_id == point.id))
    return backtest.point_dict(point, trial, include_snapshot=True)


@router.get('/cases/{id}/download')
def download_case(id: int, db=Depends(get_db), user=Depends(current_user)):
    case = db.get(BacktestCase, id)
    if not case:
        raise HTTPException(404, '回测案不存在')
    payload = backtest.build_export(db, case)
    data = json.dumps(payload, ensure_ascii=False, allow_nan=False, indent=2).encode()
    record(db, user.email, 'backtest.case.download', {'case': id, 'bytes': len(data),
                                                      'checksum': hashlib.sha256(data).hexdigest()})
    db.commit()
    return Response(data, media_type='application/json',
                    headers={'Content-Disposition': f'attachment; filename="backtest-case-{id}.json"',
                             'X-Content-SHA256': hashlib.sha256(data).hexdigest()})


@router.get('/cases/{id}/derivation.csv')
def derivation_csv(id: int, db=Depends(get_db), user=Depends(current_user)):
    """逐时点纳入口径清单：时点、读者、采用修订与接收/到期时间，用于人工核对。"""
    case = db.get(BacktestCase, id)
    if not case:
        raise HTTPException(404, '回测案不存在')
    backtest.require_complete(db, case)
    points = list(db.scalars(select(backtest.BacktestPoint).where(backtest.BacktestPoint.case_id == id)
                             .order_by(backtest.BacktestPoint.sequence)))
    out = io.StringIO()
    out.write('﻿')
    writer = csv.writer(out)
    writer.writerow(['时点序号', '回测时点', '读者', '采用修订', '续读标签',
                     '业务发生时间', '标签到期时间', '接收时间', '内容指纹'])
    def safe(value):
        text = str(value)
        return "'" + text if text.startswith(('=', '+', '-', '@', '\t', '\r')) else text
    for point in points:
        for decision in point.decisions or []:
            writer.writerow([point.sequence, backtest.iso(point.cutoff_at), safe(decision['reader_id']),
                             decision['revision'], decision.get('renewed', ''), backtest.iso(decision['occurred_at']),
                             backtest.iso(decision['matured_at']), backtest.iso(decision['received_at']),
                             point.snapshot_checksum])
    data = out.getvalue().encode('utf-8')
    record(db, user.email, 'backtest.case.derivation', {'case': id})
    db.commit()
    return Response(data, media_type='text/csv; charset=utf-8',
                    headers={'Content-Disposition': f'attachment; filename="backtest-case-{id}-derivation.csv"'})


@router.post('/briefings', status_code=201)
def create_briefing(data: BacktestBriefingInput, db=Depends(get_db), user=Depends(admin_only)):
    case = db.get(BacktestCase, data.case_id)
    if not case:
        raise HTTPException(404, '回测案不存在')
    backtest.require_complete(db, case)
    if not case.approved:
        raise HTTPException(409, '回测案须先获批准，才能生成内部简报')
    payload = backtest.build_export(db, case)
    payload['briefing'] = {'name': data.name, 'destination': data.destination,
                           'interval_seconds': data.interval_seconds, 'created_by': user.email}
    content = json.dumps(payload, ensure_ascii=False, allow_nan=False, indent=2).encode()
    checksum = hashlib.sha256(content).hexdigest()
    os.makedirs(ARTIFACT_DIR, exist_ok=True)
    filename = f'backtest-briefing-case-{case.id}.json'
    with open(os.path.join(ARTIFACT_DIR, filename), 'wb') as handle:
        handle.write(content)
    briefing = BacktestBriefing(case_id=case.id, name=data.name, destination=data.destination,
                                filename=filename, checksum=checksum, created_by=user.email,
                                created_at=time.time())
    db.add(briefing)
    record(db, user.email, 'backtest.briefing.create', {'case': case.id, 'briefing': filename,
                                                        'checksum': checksum,
                                                        'next_run': time.time() + data.delay_seconds})
    db.commit()
    return _briefing_dict(briefing)


@router.get('/briefings')
def list_briefings(db=Depends(get_db), user=Depends(current_user)):
    return [_briefing_dict(b) for b in db.scalars(select(BacktestBriefing).order_by(BacktestBriefing.id.desc()))]


@router.get('/briefings/{id}/download')
def download_briefing(id: int, db=Depends(get_db), user=Depends(current_user)):
    briefing = db.get(BacktestBriefing, id)
    if not briefing:
        raise HTTPException(404, '简报归档不存在')
    path = os.path.join(ARTIFACT_DIR, briefing.filename)
    if not os.path.exists(path):
        raise HTTPException(404, '简报文件不存在，请重新生成')
    return FileResponse(path, filename=briefing.filename, media_type='application/json')
