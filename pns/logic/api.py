# pns/logic/api.py
import os
import json
from typing import Optional
import aiohttp
import asyncio

class MiMoAPI:
    """MiMo API客户端"""
    
    def __init__(self, base_url: str = None, model: str = "mimo-v2.5-pro"):
        self.base_url = base_url or os.getenv('MIMO_BASE_URL', 'https://api.mimoai.com/v1')
        self.api_key = os.getenv('MIMO_API_KEY')
        self.model = model
        
        if not self.api_key:
            raise ValueError("MIMO_API_KEY not set in environment")
    
    async def call(self, prompt: str, system: str, max_tokens: int = 500) -> str:
        """异步调用MiMo API"""
        headers = {
            'Authorization': f'Bearer {self.api_key}',
            'Content-Type': 'application/json',
        }
        
        payload = {
            'model': self.model,
            'messages': [
                {'role': 'system', 'content': system},
                {'role': 'user', 'content': prompt},
            ],
            'max_tokens': max_tokens,
            'temperature': 0.8,
        }
        
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f'{self.base_url}/messages',
                headers=headers,
                json=payload,
                timeout=30
            ) as resp:
                if resp.status != 200:
                    error_text = await resp.text()
                    raise Exception(f"MiMo API error {resp.status}: {error_text}")
                
                data = await resp.json()
                return data['content'][0]['text']
    
    def call_sync(self, prompt: str, system: str, max_tokens: int = 500) -> str:
        """同步调用MiMo API（CLI使用）"""
        loop = asyncio.get_event_loop()
        return loop.run_until_complete(self.call(prompt, system, max_tokens))


# 全局实例
_mimo_client: Optional[MiMoAPI] = None

def get_mimo_client() -> MiMoAPI:
    """获取MiMo客户端单例"""
    global _mimo_client
    if _mimo_client is None:
        _mimo_client = MiMoAPI()
    return _mimo_client

def call_mimo_api(prompt: str, system: str, max_tokens: int = 500) -> str:
    """便捷函数：同步调用MiMo API"""
    client = get_mimo_client()
    return client.call_sync(prompt, system, max_tokens)
