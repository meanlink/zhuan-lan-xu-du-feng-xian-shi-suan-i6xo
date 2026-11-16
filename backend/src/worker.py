import time, json, logging
from pathlib import Path
from sqlalchemy import select
from redis import Redis
from src.config.database import Session
from src.config.settings import REDIS_URL
from src.models import Job, Dataset, Report, BacktestCase, BacktestPoint
from src.services.business import analyze
from src.services.reports import deliver
from src.services.audit import record
from src.services.backtest import run_case, refresh_case_status
logging.basicConfig(level=logging.INFO)
def execute(job_id):
    with Session() as db:
        j=db.get(Job,job_id)
        if not j or j.status!='queued':return
        j.status='running'; db.commit()
        try:
            d=db.get(Dataset,j.dataset_id)
            result=analyze(d.records,j.params)
            json.dumps(result,allow_nan=False)
            j.result=result;j.status='completed';j.finished_at=time.time()
            record(db,'worker','job.completed',{'id':j.id})
        except Exception as e:
            logging.exception('Analysis failed')
            j.status='failed';j.error=str(e)[:990];j.finished_at=time.time()
            record(db,'worker','job.failed',{'id':j.id,'error':j.error})
        db.commit()
def execute_case(case_id):
    with Session() as db:
        case=db.get(BacktestCase,case_id)
        if not case or case.approved:return
        remaining=db.scalar(select(BacktestPoint.id).where(BacktestPoint.case_id==case_id,BacktestPoint.status!='completed').limit(1))
        if not remaining:return
        try:
            run_case(db,case)
            refresh_case_status(db,case)
            record(db,'worker','backtest.case.finished',{'case':case.id,'status':case.status})
        except Exception as e:
            logging.exception('Backtest failed')
            case.status='partial'
            db.commit()
            record(db,'worker','backtest.case.failed',{'case':case.id,'error':str(e)[:900]})
        db.commit()

def tick():
    with Session() as db:
        queued=list(db.scalars(select(Job.id).where(Job.status=='queued').limit(3)))
    for job_id in queued:execute(job_id)
    # 队列丢失时仍可从数据库恢复：以未完成时点为准，而不是只看案状态，
    # 避免案状态与实际时点进度不一致时漏跑。
    with Session() as db:
        unfinished=list(db.scalars(select(BacktestPoint.case_id).where(
            BacktestPoint.status.in_(['queued','running'])).distinct().limit(3)))
        headed=list(db.scalars(select(BacktestCase.id).where(
            BacktestCase.status.in_(['queued','running'])).limit(3)))
    for case_id in list(dict.fromkeys(list(unfinished)+list(headed))):execute_case(case_id)
    with Session() as db:
        for r in db.scalars(select(Report).where(Report.enabled==True,Report.next_run<=time.time())):
            d=deliver(db,r)
            if d:record(db,'worker','report.delivered',{'id':r.id,'delivery':d.id})
        db.commit()
def main():
    r=Redis.from_url(REDIS_URL,socket_timeout=3)
    # This Compose stack has a single worker. A crash must not strand its jobs.
    with Session() as db:
        for j in db.scalars(select(Job).where(Job.status=='running')):
            j.status='queued';record(db,'worker','job.recovered',{'id':j.id})
        for p in db.scalars(select(BacktestPoint).where(BacktestPoint.status=='running')):
            # 只回收中断时点；已完成的时点与试验保持原样，重试复用同一试验
            p.status='queued';record(db,'worker','backtest.point.recovered',{'id':p.id})
        db.commit()
    while True:
        Path('/tmp/heartbeat').touch()
        try:
            item=r.blpop(['analysis:queue','backtest:queue'],timeout=1)
            if item:
                if item[0]==b'backtest:queue' or item[0]==b'backtest:queue'.encode():execute_case(int(item[1]))
                else:execute(int(item[1]))
            tick()
        except Exception:
            logging.exception('Worker retry');time.sleep(2)
if __name__=='__main__':main()
