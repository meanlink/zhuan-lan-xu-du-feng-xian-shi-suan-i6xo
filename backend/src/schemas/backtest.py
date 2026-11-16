from pydantic import BaseModel, ConfigDict, Field


class HistoryBatchInput(BaseModel):
    model_config = ConfigDict(extra='forbid')

    name: str = Field(min_length=2, max_length=160)
    source: str = Field(default='history.json', max_length=200)
    note: str = Field(default='', max_length=500)
    records: list[dict] = Field(min_length=1, max_length=20000)


class BacktestCaseInput(BaseModel):
    model_config = ConfigDict(extra='forbid')

    name: str = Field(min_length=2, max_length=160)
    receive_cutoff: str
    points: list[str] = Field(min_length=1, max_length=24)
    batch_ids: list[int] | None = None
    params: dict = Field(default_factory=dict)


class BacktestBriefingInput(BaseModel):
    case_id: int
    name: str = Field(min_length=2, max_length=160)
    destination: str = Field(default='业务复核组 · 本地收件箱', min_length=2, max_length=120)
    interval_seconds: int = Field(default=86400, ge=60, le=2592000)
    delay_seconds: int = Field(default=0, ge=0, le=2592000)
