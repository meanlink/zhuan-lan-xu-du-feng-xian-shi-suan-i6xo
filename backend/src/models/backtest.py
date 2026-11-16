from sqlalchemy import String, JSON, Double, Integer, ForeignKey, UniqueConstraint, Index, Boolean
from sqlalchemy.orm import Mapped, mapped_column
from src.config.database import Base


class HistoryBatch(Base):
    """一次历史资料接收。received_at 只能由服务端在接收完成时写入。"""

    __tablename__ = 'history_batches'
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(160), unique=True)
    source: Mapped[str] = mapped_column(String(200), default='history.json')
    note: Mapped[str] = mapped_column(String(500), default='')
    status: Mapped[str] = mapped_column(String(20), default='receiving')
    record_count: Mapped[int] = mapped_column(Integer, default=0)
    checksum: Mapped[str] = mapped_column(String(64), default='')
    received_at: Mapped[float] = mapped_column(Double)
    created_by: Mapped[int] = mapped_column(ForeignKey('users.id'))
    created_at: Mapped[float] = mapped_column(Double)


class HistorySample(Base):
    """读者行为样本的一次修订；区分业务发生时间与实际接收时间。"""

    __tablename__ = 'history_samples'
    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey('history_batches.id'))
    reader_id: Mapped[str] = mapped_column(String(80))
    revision: Mapped[int] = mapped_column(Integer)
    sessions_14d: Mapped[float] = mapped_column(Double)
    completion_pct: Mapped[float] = mapped_column(Double)
    days_inactive: Mapped[float] = mapped_column(Double)
    issues_read: Mapped[float] = mapped_column(Double)
    renewed: Mapped[int] = mapped_column(Integer)
    occurred_at: Mapped[float] = mapped_column(Double)
    matured_at: Mapped[float] = mapped_column(Double)
    received_at: Mapped[float] = mapped_column(Double)
    created_at: Mapped[float] = mapped_column(Double)
    __table_args__ = (
        UniqueConstraint('reader_id', 'revision', name='uq_history_reader_revision'),
        UniqueConstraint('batch_id', 'reader_id', 'revision', name='uq_history_batch_reader_revision'),
        Index('ix_history_samples_reader', 'reader_id'),
        Index('ix_history_samples_batch', 'batch_id'),
    )


class BacktestCase(Base):
    """冻结接收范围与多个回测时点的历史回测案。"""

    __tablename__ = 'backtest_cases'
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(160), unique=True)
    status: Mapped[str] = mapped_column(String(20), default='queued')
    receive_cutoff: Mapped[float] = mapped_column(Double)
    scope_batch_ids: Mapped[list] = mapped_column(JSON, default=list)
    scope_snapshot: Mapped[list] = mapped_column(JSON, default=list)
    scope_checksum: Mapped[str] = mapped_column(String(64), default='')
    points_count: Mapped[int] = mapped_column(Integer, default=0)
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    created_by: Mapped[int] = mapped_column(ForeignKey('users.id'))
    created_at: Mapped[float] = mapped_column(Double)
    started_at: Mapped[float | None] = mapped_column(Double, nullable=True)
    finished_at: Mapped[float | None] = mapped_column(Double, nullable=True)
    last_resumed_at: Mapped[float | None] = mapped_column(Double, nullable=True)
    approved: Mapped[bool] = mapped_column(Boolean, default=False)
    approved_by: Mapped[str] = mapped_column(String(150), default='')
    approved_at: Mapped[float | None] = mapped_column(Double, nullable=True)


class BacktestPoint(Base):
    """一个回测时点；纳入/排除决定在建案时按冻结范围固化。"""

    __tablename__ = 'backtest_points'
    id: Mapped[int] = mapped_column(primary_key=True)
    case_id: Mapped[int] = mapped_column(ForeignKey('backtest_cases.id'))
    sequence: Mapped[int] = mapped_column(Integer)
    cutoff_at: Mapped[float] = mapped_column(Double)
    status: Mapped[str] = mapped_column(String(20), default='queued')
    snapshot: Mapped[list] = mapped_column(JSON, default=list)
    decisions: Mapped[list] = mapped_column(JSON, default=list)
    reason_counts: Mapped[dict] = mapped_column(JSON, default=dict)
    exclusions: Mapped[list] = mapped_column(JSON, default=list)
    included_count: Mapped[int] = mapped_column(Integer, default=0)
    excluded_count: Mapped[int] = mapped_column(Integer, default=0)
    snapshot_checksum: Mapped[str] = mapped_column(String(64), default='')
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str] = mapped_column(String(1000), default='')
    started_at: Mapped[float | None] = mapped_column(Double, nullable=True)
    finished_at: Mapped[float | None] = mapped_column(Double, nullable=True)
    __table_args__ = (
        UniqueConstraint('case_id', 'sequence', name='uq_backtest_point_sequence'),
        UniqueConstraint('case_id', 'cutoff_at', name='uq_backtest_point_cutoff'),
        Index('ix_backtest_points_case', 'case_id'),
    )


class BacktestTrial(Base):
    """每个时点至多一个试验；point_id 唯一约束保证重试不重复建试验。"""

    __tablename__ = 'backtest_trials'
    id: Mapped[int] = mapped_column(primary_key=True)
    case_id: Mapped[int] = mapped_column(ForeignKey('backtest_cases.id'))
    point_id: Mapped[int] = mapped_column(ForeignKey('backtest_points.id'), unique=True)
    status: Mapped[str] = mapped_column(String(20), default='pending')
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str] = mapped_column(String(1000), default='')
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[float] = mapped_column(Double)
    started_at: Mapped[float | None] = mapped_column(Double, nullable=True)
    finished_at: Mapped[float | None] = mapped_column(Double, nullable=True)


class BacktestBriefing(Base):
    """仅由齐全且获批的回测案生成的内部简报归档。"""

    __tablename__ = 'backtest_briefings'
    id: Mapped[int] = mapped_column(primary_key=True)
    case_id: Mapped[int] = mapped_column(ForeignKey('backtest_cases.id'))
    name: Mapped[str] = mapped_column(String(160))
    destination: Mapped[str] = mapped_column(String(120))
    filename: Mapped[str] = mapped_column(String(160))
    checksum: Mapped[str] = mapped_column(String(64))
    created_by: Mapped[str] = mapped_column(String(150))
    created_at: Mapped[float] = mapped_column(Double)
