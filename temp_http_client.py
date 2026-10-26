import httpx
import logging
import asyncio

logger = logging.getLogger(__name__)

async def safe_http_request(
    http_client,
    method: str, 
    url: str, 
    headers: dict = None, 
    data: dict = None,
    json: dict = None, 
    params: dict = None, 
    follow_redirects: bool = True,
    timeout: float = None
):
    #method는 GET, POST, PATCH, DELETE
    method = method.upper()
    # httpx 트랜스포트에는 _loop 속성이 없어 루프 비교가 항상 실패했음
    # -> 열려 있으면 공유 클라이언트를 쓰고, 다른 루프 문제(RuntimeError) 등은 아래 except에서 임시 클라이언트로 전환
    if http_client is not None and not http_client.is_closed:
        try:
            return await http_client.request(
                method=method, url=url, headers=headers, data=data, json=json, params=params, follow_redirects=follow_redirects, timeout=timeout
            )
        except RuntimeError as e:
            logger.warning(f"임시 HTTP 클라이언트로 전환: {e}",exc_info=True)
        except Exception as e:
            logger.warning(f"임시 HTTP 클라이언트로 전환: {e}",exc_info=True)

    limits = httpx.Limits(max_keepalive_connections=5, max_connections=20)
    timeout = httpx.Timeout(20.0, connect=15.0)
    
    async with httpx.AsyncClient(limits=limits, timeout=timeout) as temp_client:
        
        return await temp_client.request(
            method=method, url=url, headers=headers, data=data, json=json, params=params, follow_redirects=follow_redirects, timeout=timeout
        )