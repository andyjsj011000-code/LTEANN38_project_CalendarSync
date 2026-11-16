# app/tasks/lifecycle.py
import logging
from datetime import timedelta

from sqlalchemy import delete

from app.core.database import SessionLocal
from app.core.timezone import now_kst

# UserSyncLog의 ORM 관계가 참조하는 User와 Channel을 등록한다.
from app.models import domain  # noqa: F401
from app.models.master_calendar import UserSyncLog

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


async def run_lifecycle_cleanup_task():
    """오래된 로그 정리"""

    with SessionLocal() as db:
        try:
            now = now_kst()

            # 2. 180일 이상 지난 오래된 UserSyncLog 일괄 삭제 (Bulk DELETE)
            log_cleanup_threshold = now - timedelta(days=180)

            stmt_delete_logs = delete(UserSyncLog).where(
                UserSyncLog.synced_at < log_cleanup_threshold
            )

            db.execute(stmt_delete_logs)

            # 3. 트랜잭션 반영 및 커밋
            db.commit()

            logger.info("라이프사이클 태스크 완료 ")

        except Exception as e:
            db.rollback()
            logger.error(f"라이프사이클 태스크 중 에러 발생: {e}", exc_info=True)
