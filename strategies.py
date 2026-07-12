import numpy as np
import pandas as pd

from pprint import pprint

from quantpylib.simulator.alpha import Alpha


class HARVol:
    '''Миксин: заменяет наивную 30-дневную vol движка на HAR-прогноз волатильности.
    HAR работает в post_compute, который выполняется ПОСЛЕ установки наивной vol,
    но ДО главного цикла -> перезапись подхватывается.

    har_beta = [const, b_d, b_w, b_m] — коэффициенты, обученные ВНЕ (без заглядывания).
    Применение:  class MAverageHAR(HARVol, MAverage): pass
                 MAverageHAR(har_beta=beta, insts=..., dfs=..., ...)'''

    HAR_FLOOR = 1e-4   # пол под log
    VOL_FLOOR = 0.005  # тот же пол, что в движке

    def __init__(self, har_beta, **kwargs):
        super().__init__(**kwargs)
        self.har_beta = np.asarray(har_beta, dtype=float)

    def post_compute(self, date_range):
        super().post_compute(date_range)            # сначала сигнал/eligible стратегии
        b = self.har_beta
        for inst in self.insts:
            ret = self.dfs[inst]['close'].pct_change()
            rv_d = ret.abs().clip(lower=self.HAR_FLOOR)
            rv_w = ret.rolling(5).std().clip(lower=self.HAR_FLOOR)
            rv_m = ret.rolling(22).std().clip(lower=self.HAR_FLOOR)
            har = np.exp(b[0] + b[1] * np.log(rv_d) + b[2] * np.log(rv_w) + b[3] * np.log(rv_m))
            har = har.ffill().bfill()
            self.dfs[inst]['vol'] = np.where(har < self.VOL_FLOOR, self.VOL_FLOOR, har)

class Bollinger(Alpha):
    
    def compute_forecasts(self,date,eligibles):
        forecasts = {}
        for inst in eligibles:
            forecasts[inst] = self.dfs[inst].at[date,'alpha']
        return forecasts

    def pre_compute(self,date_range):
        for inst in self.insts:
            inst_df = self.dfs[inst]
            bollinger = (inst_df['close'] - inst_df['close'].rolling(14).mean()) / inst_df['close'].rolling(14).std()
            self.dfs[inst]['alpha'] = bollinger

    def post_compute(self,date_range):
        for inst in self.insts:
            self.dfs[inst]['alpha'] = self.dfs[inst]['alpha'].ffill()
            self.dfs[inst]['eligible'] = self.dfs[inst]['eligible'] & \
                (~pd.isna(self.dfs[inst]['alpha']))

class MAverage(Alpha):

    def compute_forecasts(self,date,eligibles):
        forecasts = {}
        for inst in eligibles:
            forecasts[inst] = self.dfs[inst].at[date,'alpha']
        return forecasts

    def pre_compute(self,date_range):
        for inst in self.insts:
            inst_df = self.dfs[inst]
            trending_0 = np.where(inst_df.close.rolling(10).mean() > inst_df.close.rolling(20).mean(),1,-1)
            trending_1 = np.where(inst_df.close.rolling(15).mean() > inst_df.close.rolling(30).mean(),1,-1)
            trending_2 = np.where(inst_df.close.rolling(20).mean() > inst_df.close.rolling(50).mean(),1,-1)
            trending = trending_0 + trending_1 + trending_2
            trending = trending.astype(np.float64)
            trending[0:50] = np.nan
            self.dfs[inst]['alpha'] = trending

    def post_compute(self,date_range):
        for inst in self.insts:
            self.dfs[inst]['alpha'] = self.dfs[inst]['alpha'].ffill()
            self.dfs[inst]['eligible'] = self.dfs[inst]['eligible'] & \
                (~pd.isna(self.dfs[inst]['alpha']))


class Donchian(Alpha):
    '''Пробой канала Дончиана (25д): лонг на новом максимуме, шорт на минимуме, держим до разворота.'''

    def compute_forecasts(self,date,eligibles):
        forecasts = {}
        for inst in eligibles:
            forecasts[inst] = self.dfs[inst].at[date,'alpha']
        return forecasts

    def pre_compute(self,date_range):
        n = 25
        for inst in self.insts:
            c = self.dfs[inst]['close']
            hi = c.rolling(n).max()
            lo = c.rolling(n).min()
            alpha = np.where(c >= hi, 1.0, np.where(c <= lo, -1.0, np.nan))
            self.dfs[inst]['alpha'] = alpha

    def post_compute(self,date_range):
        for inst in self.insts:
            self.dfs[inst]['alpha'] = self.dfs[inst]['alpha'].ffill()  # держим последний пробой
            self.dfs[inst]['eligible'] = self.dfs[inst]['eligible'] & \
                (~pd.isna(self.dfs[inst]['alpha']))


class Reversion(Alpha):
    '''Краткосрочный разворот (5д): лонг недавних лузеров, шорт недавних лидеров.'''

    def compute_forecasts(self,date,eligibles):
        forecasts = {}
        for inst in eligibles:
            forecasts[inst] = self.dfs[inst].at[date,'alpha']
        return forecasts

    def pre_compute(self,date_range):
        k = 5
        for inst in self.insts:
            c = self.dfs[inst]['close']
            self.dfs[inst]['alpha'] = -(c / c.shift(k) - 1)  # против недавнего движения

    def post_compute(self,date_range):
        for inst in self.insts:
            self.dfs[inst]['alpha'] = self.dfs[inst]['alpha'].ffill()
            self.dfs[inst]['eligible'] = self.dfs[inst]['eligible'] & \
                (~pd.isna(self.dfs[inst]['alpha']))


class XSMomentum(Alpha):
    '''Кросс-секционный моментум (30д): лонг относительных лидеров, шорт аутсайдеров.
    Сигнал считается ПОСЛЕ выравнивания на общий календарь (в post_compute),
    т.к. нужен срез по всем инструментам на каждую дату.'''

    def compute_forecasts(self,date,eligibles):
        forecasts = {}
        for inst in eligibles:
            forecasts[inst] = self.dfs[inst].at[date,'alpha']
        return forecasts

    def pre_compute(self,date_range):
        pass

    def post_compute(self,date_range):
        n = 30
        mom = pd.DataFrame({
            inst: self.dfs[inst]['close'] / self.dfs[inst]['close'].shift(n) - 1
            for inst in self.insts
        })
        ranks = mom.rank(axis=1)                       # ранг по рынку (робастно к выбросам)
        demeaned = ranks.sub(ranks.mean(axis=1), axis=0)  # центрируем: лидеры >0, аутсайдеры <0
        for inst in self.insts:
            self.dfs[inst]['alpha'] = demeaned[inst].ffill()
            self.dfs[inst]['eligible'] = self.dfs[inst]['eligible'] & \
                (~pd.isna(self.dfs[inst]['alpha']))


def tema(series, span):
    '''Triple Exponential MA: TEMA = 3*EMA1 - 3*EMA2 + EMA3. Меньше лага, чем SMA/EMA.'''
    e1 = series.ewm(span=span, adjust=False).mean()
    e2 = e1.ewm(span=span, adjust=False).mean()
    e3 = e2.ewm(span=span, adjust=False).mean()
    return 3 * e1 - 3 * e2 + e3


class TEMAverage(Alpha):
    '''То же, что MAverage, но кроссоверы считаются на TEMA вместо SMA.'''

    def compute_forecasts(self,date,eligibles):
        forecasts = {}
        for inst in eligibles:
            forecasts[inst] = self.dfs[inst].at[date,'alpha']
        return forecasts

    def pre_compute(self,date_range):
        for inst in self.insts:
            c = self.dfs[inst]['close']
            trending_0 = np.where(tema(c, 10) > tema(c, 20), 1, -1)
            trending_1 = np.where(tema(c, 15) > tema(c, 30), 1, -1)
            trending_2 = np.where(tema(c, 20) > tema(c, 50), 1, -1)
            trending = (trending_0 + trending_1 + trending_2).astype(np.float64)
            trending[0:50] = np.nan
            self.dfs[inst]['alpha'] = trending

    def post_compute(self,date_range):
        for inst in self.insts:
            self.dfs[inst]['alpha'] = self.dfs[inst]['alpha'].ffill()
            self.dfs[inst]['eligible'] = self.dfs[inst]['eligible'] & \
                (~pd.isna(self.dfs[inst]['alpha']))


class RSI(Alpha):
    '''RSI(14) как ТРЕНДОВЫЙ сигнал (centerline momentum): RSI>50 -> лонг, <50 -> шорт.
    Классический "перекуплен->шорт" в крипте теряет (она трендит), поэтому берём моментум.'''

    def compute_forecasts(self,date,eligibles):
        forecasts = {}
        for inst in eligibles:
            forecasts[inst] = self.dfs[inst].at[date,'alpha']
        return forecasts

    def pre_compute(self,date_range):
        n = 14
        for inst in self.insts:
            delta = self.dfs[inst]['close'].diff()
            gain = delta.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()      # сглаживание Уайлдера
            loss = (-delta.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
            rs = gain / (loss + 1e-12)
            rsi = 100 - 100 / (1 + rs)
            self.dfs[inst]['alpha'] = rsi - 50                                    # >0 лонг, <0 шорт

    def post_compute(self,date_range):
        for inst in self.insts:
            self.dfs[inst]['alpha'] = self.dfs[inst]['alpha'].ffill()
            self.dfs[inst]['eligible'] = self.dfs[inst]['eligible'] & \
                (~pd.isna(self.dfs[inst]['alpha']))


class MACD(Alpha):
    '''MACD(12,26,9): лонг когда MACD-линия выше сигнальной, иначе шорт (трендовый крест).'''

    def compute_forecasts(self,date,eligibles):
        forecasts = {}
        for inst in eligibles:
            forecasts[inst] = self.dfs[inst].at[date,'alpha']
        return forecasts

    def pre_compute(self,date_range):
        for inst in self.insts:
            c = self.dfs[inst]['close']
            macd = c.ewm(span=12, adjust=False).mean() - c.ewm(span=26, adjust=False).mean()
            signal = macd.ewm(span=9, adjust=False).mean()
            trend = np.where(macd > signal, 1.0, -1.0)
            trend[0:26] = np.nan                                                  # прогрев медленной EMA
            self.dfs[inst]['alpha'] = trend

    def post_compute(self,date_range):
        for inst in self.insts:
            self.dfs[inst]['alpha'] = self.dfs[inst]['alpha'].ffill()
            self.dfs[inst]['eligible'] = self.dfs[inst]['eligible'] & \
                (~pd.isna(self.dfs[inst]['alpha']))
