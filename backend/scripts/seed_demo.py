import time, json
from sqlalchemy import select, func
from src.config.database import Session
from migrations.initial import migrate
from src.models import User, Dataset, Job, Report, Delivery, Audit, HistoryBatch, HistorySample, BacktestCase, BacktestPoint, BacktestTrial
from src.services.business import metadata, seed_records, analyze
from src.services.reports import deliver
from src.utils.security import hash_password
from src.utils.serialization import checksum
from scripts.backtest_seed_data import generate as generate_history
from src.services import backtest
def seed():
    migrate()
    with Session() as db:
        for role,name in [('admin','林知 · 业务负责人'),('analyst','陈舟 · 分析员'),('viewer','周禾 · 观察员')]:
            email=role+'@demo.local'
            if not db.scalar(select(User).where(User.email==email)):
                db.add(User(email=email,name=name,role=role,password=hash_password('Demo123!')))
        db.flush()
        m=metadata()
        d=db.scalar(select(Dataset).where(Dataset.name==m['dataset_name']))
        if not d:
            rows=seed_records()
            d=Dataset(name=m['dataset_name'],source=m['short']+'-202611.csv',records=rows,checksum=checksum(rows),created_at=time.time())
            db.add(d); db.flush()
        if not db.scalar(select(Job).where(Job.name=='基线复核 · '+m['name'])):
            params={p['key']:p['default'] for p in m.get('parameters',[])}
            result=analyze(d.records,params)
            json.dumps(result,ensure_ascii=False,allow_nan=False)
            admin=db.scalar(select(User).where(User.role=='admin'))
            j=Job(name='基线复核 · '+m['name'],dataset_id=d.id,params=params,status='completed',result=result,created_by=admin.id,created_at=time.time()-3600,finished_at=time.time()-3595,approved=True)
            db.add(j);db.flush()
            r=Report(name='每日业务复核简报',job_id=j.id,destination='业务复核组 · 本地收件箱',interval_seconds=86400,next_run=time.time()+86400,enabled=True)
            db.add(r);db.flush();deliver(db,r)
            db.add(Audit(actor='seed',action='demo.baseline',detail={'dataset':d.id},at=time.time()))
        seed_history(db)
        db.commit()
        counts={model.__tablename__:db.scalar(select(func.count()).select_from(model)) for model in [User,Dataset,Job,Report,Delivery,Audit,HistoryBatch,HistorySample,BacktestCase,BacktestPoint,BacktestTrial]}
        print(json.dumps(counts,ensure_ascii=False))
        return counts
def seed_history(db):
    """幂等写入历史资料批次，并建立一个已完成的历史回测案示例。"""
    admin=db.scalar(select(User).where(User.role=='admin'))
    if db.scalar(select(func.count()).select_from(HistoryBatch)):
        return
    for spec in generate_history():
        rows=[{k:v for k,v in row.items() if k!='cohort'} for row in spec['rows']]
        backtest.receive_batch(db,type('Batch',(),{
            'name':spec['name'],'source':spec['source'],'note':spec['note'],'records':rows
        })(),admin,now=spec['received_at'])
    db.commit()
    # 用与服务一致的方式冻结接收范围和各时点，再逐点执行。
    from src.schemas.backtest import BacktestCaseInput
    case,_=backtest.plan_case(db,BacktestCaseInput(
        name='2025 续读结果历史回测',receive_cutoff='2025-12-31T23:59:59+00:00',
        points=['2025-03-31','2025-06-30','2025-09-30','2025-12-31'],
        params={'threshold':0.5,'regularization':1.0}),admin)
    case.created_at=time.time()-7200
    backtest.run_case(db,case)
    case.approved=True
    case.approved_by=admin.email if admin else 'seed'
    case.approved_at=time.time()-7000
    db.add(Audit(actor='seed',action='backtest.case.seeded',detail={'case':case.id,'points':case.points_count},at=time.time()))


if __name__=='__main__':seed()
