from src.database import Base
from src.models.audit_models import Customer, CustomerAlias, ClusterAbbr, CashAuditLog, Receivable, BankLedgerDB

def init_db():
    Base.metadata.create_all(bind=engine)