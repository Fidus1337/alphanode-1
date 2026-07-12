import pytz
import numpy as np
import pandas as pd

from quantpylib.throttler.aiosonic import HTTPClient
from quantpylib.throttler.rate_semaphore import AsyncRateSemaphore
from quantpylib.throttler.decorators import aconsume_credits


USDM_BASE_URL = "https://fapi.binance.com"

def map_to_bin_granularities(granularity,granularity_multiplier):
    if granularity == 'm':
        return f'{granularity_multiplier}m', granularity_multiplier * 60
    elif granularity == 'h':
        return f'{granularity_multiplier}h', granularity_multiplier * 60 * 60
    elif granularity == 'd':
        return f'{granularity_multiplier}d', granularity_multiplier * 60 * 60 * 24
    elif granularity == 'w':
        return f'{granularity_multiplier}w', granularity_multiplier * 60 * 60 * 24 * 7
    elif granularity == 'M':
        return f'{granularity_multiplier}M', granularity_multiplier * 60 * 60 * 24 * 31
    else:
        raise ValueError('Invalid granularity')

endpoints = {
    'kline_candlestick': {
        'endpoint': '/fapi/v1/klines',
        'method': 'GET',
    },
    'exchange_info': {
        'endpoint': '/fapi/v1/exchangeInfo',
        'method': 'GET',
    }
}

class Binance():
    
    def __init__(self,key='',secret='',**kwargs):
        self.key = key
        self.secret = secret
        self.http_client = HTTPClient(base_url=USDM_BASE_URL)
        self.rate_semaphore = AsyncRateSemaphore(6000)

    async def exchange_info(self):
        request = dict(endpoints['exchange_info'])
        return await self.http_client.request(**request)

    async def get_trade_bars(self,ticker,start,end,granularity,granularity_multiplier,kline_close=False):
        gran_str, gran_secs = map_to_bin_granularities(granularity,granularity_multiplier)
        if start.tzinfo is None: start=start.replace(tzinfo=pytz.utc)
        if end.tzinfo is None: end=end.replace(tzinfo=pytz.utc)
        unix_start,unix_end = int(start.timestamp()*1000),int(end.timestamp()*1000)
        results = []
        while unix_start < unix_end:
            res = await self.kline_candlestick(
                symbol=ticker,
                interval=gran_str,
                startTime=unix_start,
                endTime=unix_start + gran_secs * 1000 * 1490,
                limit=1500
            )
            if len(res) == 0 and len(results) == 0:
                unix_start = unix_start + gran_secs * 1000
                continue
            if len(res) == 0 or unix_start == res[-1][0]:
                break
            else:
                unix_start = res[-1][0]
            results.extend(res)
        df = pd.DataFrame(results)
        df.columns=['t','open','high','low','close','volume','T','-','-','-','-','-']
        if df.empty:
            return pd.DataFrame()
        dt_col = {"T":'datetime'} if kline_close else {"t":'datetime'}
        df = df.rename(columns=dt_col)
        df = df.drop(columns=[col for col in list(df) if col not in ['datetime','open','high','low','close','volume']])         
        df.datetime = pd.to_datetime(df.datetime,unit='ms',utc=True)
        df = df.set_index('datetime',drop=True)
        df = df[~df.index.duplicated(keep='last')]
        df = df[start:end]
        df[['open','high','low','close','volume']] = df[['open','high','low','close','volume']].astype(np.float64)
        return df

    @aconsume_credits(costs=20,refund_in=60)
    async def kline_candlestick(self, symbol, interval, startTime=None, endTime=None, limit=None):
        params = {
            'symbol': symbol,
            'interval': interval,
            'startTime': startTime,
            'endTime': endTime,
            'limit': limit
        }
        params = {k:v for k,v in params.items() if v is not None}
        request = dict(endpoints['kline_candlestick'])
        request['params'] = params
        return await self.http_client.request(**request)

    