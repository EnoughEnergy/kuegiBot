from typing import List
import math
import plotly.graph_objects as go

from kuegi_bot.bots.MultiStrategyBot import Strategy
from kuegi_bot.bots.trading_bot import TradingBot
from kuegi_bot.kuegi_channel import KuegiChannel, Data
from kuegi_bot.utils.trading_classes import Bar, Account, Symbol, OrderType


class ChannelStrategy(Strategy):

    def __init__(self):
        super().__init__()
        self.channel: KuegiChannel = None
        self.risk_factor = 1
        self.risk_type = 0  # 0= all equal, 1= 1 atr eq 1 R
        self.max_risk_mul = 1
        self.be_factor = 0
        self.be_buffer = 0
        self.trail_to_swing = False
        self.delayed_swing_trail = True
        self.trail_back = False
        self.trail_active = False

    def myId(self):
        return "ChannelStrategy"

    def withRM(self, risk_factor: float = 0.01, max_risk_mul: float = 2, risk_type: int = 0):
        self.risk_factor = risk_factor
        self.risk_type = risk_type  # 0= all equal, 1= 1 atr eq 1 R
        self.max_risk_mul = max_risk_mul
        return self

    def withChannel(self, max_look_back, threshold_factor, buffer_factor, max_dist_factor, max_swing_length):
        self.channel = KuegiChannel(max_look_back, threshold_factor, buffer_factor, max_dist_factor, max_swing_length)
        return self

    def withBE(self, factor, buffer):
        self.be_factor = factor
        self.be_buffer = buffer
        return self

    def withTrail(self, trail_to_swing: bool = False, delayed_swing: bool = True, trail_back: bool = False):
        self.trail_active = True
        self.delayed_swing_trail = delayed_swing
        self.trail_to_swing = trail_to_swing
        self.trail_back = trail_back
        return self

    def init(self, bars: List[Bar], account: Account, symbol: Symbol):
        super().init(bars, account, symbol)
        if self.channel is None:
            self.logger.error("No channel provided on init")
        else:
            self.logger.info("init with %i %.1f %.3f %.1f %i | %.3f %.1f %i | %.1f %.1f | %s %s %s %s" %
                             (self.channel.max_look_back, self.channel.threshold_factor, self.channel.buffer_factor,
                              self.channel.max_dist_factor, self.channel.max_swing_length,
                              self.risk_factor, self.max_risk_mul, self.risk_type,
                              self.be_factor, self.be_buffer,
                              self.trail_active, self.delayed_swing_trail, self.trail_to_swing, self.trail_back))
            self.channel.on_tick(bars)

    def min_bars_needed(self) -> int:
        return self.channel.max_look_back + 1

    def got_data_for_position_sync(self, bars: List[Bar]) -> bool:
        return self.channel.get_data(bars[1]) is not None

    def prep_bars(self, is_new_bar: bool, bars: list):
        if is_new_bar:
            self.channel.on_tick(bars)

    def manage_open_order(self, order, position, bars, to_update, to_cancel, open_positions):
        last_data: Data = self.channel.get_data(bars[2])
        data: Data = self.channel.get_data(bars[1])
        if data is not None:
            stopLong = data.longTrail
            stopShort = data.shortTrail
            if self.trail_to_swing and \
                    data.longSwing is not None and data.shortSwing is not None and \
                    (not self.delayed_swing_trail or (last_data is not None and
                                                      last_data.longSwing is not None and
                                                      last_data.shortSwing is not None)):
                stopLong = max(data.shortSwing, stopLong)
                stopShort = min(data.longSwing, stopShort)

            orderType = TradingBot.order_type_from_order_id(order.id)
            if position is not None and orderType == OrderType.SL:
                # trail
                newStop = order.stop_price
                isLong = position.amount > 0
                if self.trail_active:
                    newStop = self.__trail_stop(direction=1 if isLong else -1,
                                                current_stop=newStop,
                                                trail=stopLong if isLong else stopShort,
                                                initial_stop=position.initial_stop)

                if self.be_factor > 0 and position.wanted_entry is not None and position.initial_stop is not None:
                    entry_diff = (position.wanted_entry - position.initial_stop)
                    ep = bars[0].high if isLong else bars[0].low
                    if (ep - (position.wanted_entry + entry_diff * self.be_factor)) * position.amount > 0:
                        newStop = self.__trail_stop(direction=1 if isLong else -1,
                                                    current_stop=newStop,
                                                    trail=position.wanted_entry + entry_diff * self.be_buffer,
                                                    initial_stop=position.initial_stop, only_forward= True)
                if newStop != order.stop_price:
                    order.stop_price = newStop
                    to_update.append(order)

    def add_to_plot(self, fig: go.Figure, bars: List[Bar], time):
        super().add_to_plot(fig, bars, time)
        lines = self.channel.get_number_of_lines()
        styles = self.channel.get_line_styles()
        names = self.channel.get_line_names()
        offset = 1  # we take it with offset 1
        self.logger.info("adding channel")
        for idx in range(0, lines):
            sub_data = list(map(lambda b: self.channel.get_data_for_plot(b)[idx], bars))
            fig.add_scatter(x=time, y=sub_data[offset:], mode='lines', line=styles[idx],
                            name=self.channel.id + "_" + names[idx])

    ####################################

    def __trail_stop(self, direction, current_stop, trail, initial_stop,only_forward= False):
        # direction should be > 0 for long and < 0 for short
        if (trail - current_stop) * direction > 0 or \
                (not only_forward and self.trail_back
                 and initial_stop is not None and (trail - initial_stop) * direction > 0):
            return math.floor(trail) if direction < 0 else math.ceil(trail)
        else:
            return current_stop

    def calc_pos_size(self, risk, entry, exitPrice, data: Data):
        if self.risk_type <= 2:
            delta = entry - exitPrice
            if self.risk_type == 1:
                # use atr as delta reference, but max X the actual delta. so risk is never more than X times the
                # wanted risk
                delta = math.copysign(min(self.max_risk_mul * abs(delta), self.max_risk_mul * data.atr), delta)

            if not self.symbol.isInverse:
                size = risk / delta
            else:
                size = -int(risk / (1 / entry - 1 / (entry - delta)))
            return size
