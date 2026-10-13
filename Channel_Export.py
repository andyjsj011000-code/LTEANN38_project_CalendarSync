# channel_export.py
import os
import msal
import httpx
from dotenv import load_dotenv
import logging
from datetime import datetime
from temp_http_client import safe_http_request

load_dotenv()

TENANT_ID = os.getenv('TENANT_ID')
CLIENT_ID = os.getenv('CLIENT_ID')
CLIENT_SECRET = os.getenv('CLIENT_SECRET')
'''
logger = logging.getLogger("ScheduleBot")
'''

async def get_graph_access_token() -> str | None:
    if not TENANT_ID or not CLIENT_ID or not CLIENT_SECRET:
        return None

    app = msal.ConfidentialClientApplication(
        client_id=CLIENT_ID,
        authority=f"https://login.microsoftonline.com/{TENANT_ID}",
        client_credential=CLIENT_SECRET,
    )
    
    # msal의 동기 네트워크 요청을 별도 스레드로 격리하여 이벤트 루프 마비 방지
    import asyncio
    result = await asyncio.to_thread(
        app.acquire_token_for_client,
        scopes=["https://graph.microsoft.com/.default"]
    )

    token = result.get("access_token")
    if not token:
        '''
        logger.error(f"[토큰 발급 실패] {result.get('error_description')}", exc_info=True)
        '''
    return token


async def channel_export(TEAM_ID: str, CHANNEL_ID: str, last_sync_time, client: httpx.AsyncClient, access_token: str = None):
    """채널 메시지를 가져오되, 마지막 동기화된 시점을 만나면 즉시 중단합니다."""
    if not access_token:
        access_token = await get_graph_access_token()
        
    if not access_token:
        raise RuntimeError("Graph access token unavailable for channel export")
    
    new_messages = []
    
    # 1. $filter 제거하고 기본 최신순($top=20 또는 50)으로 요청
    url = f"https://graph.microsoft.com/v1.0/teams/{TEAM_ID}/channels/{CHANNEL_ID}/messages?$top=50"
    
    stop = False
    
    while url and not stop:
        try:
            response = await safe_http_request(
                client,
                "GET",
                url, 
                headers={"Authorization": f"Bearer {access_token}"}
            )
            response.raise_for_status()
            data = response.json()
            messages = data.get("value", [])
            
            for msg in messages:
                # Graph orders threads by their latest modification, including replies.
                # An old root with a new reply must not hide newer roots on later pages.
                modified_str = msg.get("lastModifiedDateTime")
                if modified_str:
                    modified_dt = datetime.fromisoformat(modified_str.replace("Z", "+00:00"))
                    if modified_dt <= last_sync_time:
                        stop = True
                        break
                created_dt = datetime.fromisoformat(msg['createdDateTime'].replace("Z", "+00:00"))
                
                # 원글의 작성 시각이 동기화 기준 이후인 경우에만 처리합니다.
                if created_dt > last_sync_time:
                    new_messages.append(msg)
                
            url = data.get("@odata.nextLink")
        except Exception:
            logging.getLogger("ScheduleBot").exception(
                "[Graph API Error] 채널 메시지 조회 실패 (team=%s, channel=%s)", TEAM_ID, CHANNEL_ID
            )
            raise
    return new_messages
