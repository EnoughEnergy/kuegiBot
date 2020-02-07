from kuegi_bot.utils.trading_classes import Bar, Position, Symbol, OrderInterface, Account, OrderType, Order

import plotly.graph_objects as go

from typing import List
from datetime import datetime
from random import randint
from enum import Enum

import os
import json
import csv


class PositionDirection(Enum):
    LONG = "long",
    SHORT = "short"


class TradingBot:
    def __init__(self, logger, directionFilter: int = 0):
        self.myId = "GenericBot"
        self.logger = logger
        self.directionFilter = directionFilter
        self.order_interface: OrderInterface = None
        self.symbol: Symbol = None
        self.unique_id: str = ""
        self.last_time = 0
        self.last_tick_time = 0
        self.is_new_bar = True
        self.open_positions = {}
        self.known_order_history = 0
        self.position_history: List[Position] = []
        self.reset()

    def uid(self) -> str:
        return self.myId

    def prepare(self, logger, order_interface):
        self.logger = logger
        self.order_interface = order_interface

    def min_bars_needed(self):
        return 5

    def reset(self):
        self.last_time = 0
        self.open_positions = {}
        self.known_order_history = 0
        self.position_history = []

    def _get_pos_file(self):
        return self.symbol.symbol + "_" + self.unique_id + ".json" if self.unique_id is not None else None

    def init(self, bars: List[Bar], account: Account, symbol: Symbol, unique_id: str = ""):
        '''init open position etc.'''
        self.symbol = symbol
        self.unique_id = unique_id
        if unique_id is not None:
            base = 'openPositions/'
            try:
                os.makedirs(base)
            except Exception:
                pass
            try:
                with open(base + self._get_pos_file(), 'r') as file:
                    data = json.load(file)
                    self.last_time = data["last_time"]
                    for pos_json in data["positions"]:
                        pos: Position = Position.from_json(pos_json)
                        self.open_positions[pos.id] = pos
                    self.logger.info("done loading " + str(
                        len(self.open_positions)) + " positions from " + self._get_pos_file() + " last time " + str(
                        self.last_time))
            except Exception as e:
                self.logger.warn("Error loading open positions: " + str(e))
                self.open_positions = {}

        # init positions from existing orders
        self.sync_positions_with_open_orders(bars, account)

    ############### ids of pos, signal and order

    @staticmethod
    def generate_order_id(positionId: str, type: OrderType):
        if "_" in positionId:
            print("position id must not include '_' but does: " + positionId)
        orderId = positionId + "_" + str(type.name)
        if type == OrderType.SL or type == OrderType.TP:
            # add random part to prevent conflicts if the order was canceled before
            orderId = orderId + "_" + str(randint(0, 999))
        return orderId

    @staticmethod
    def position_id_from_order_id(order_id: str):
        id_parts = order_id.split("_")
        if len(id_parts) >= 1:
            return id_parts[0]
        return None

    @staticmethod
    def order_type_from_order_id(order_id: str):
        id_parts = order_id.split("_")
        if len(id_parts) >= 2:
            type = id_parts[1]
            if type == str(OrderType.ENTRY.name):
                return OrderType.ENTRY
            elif type == str(OrderType.SL.name):
                return OrderType.SL
            elif type == str(OrderType.TP.name):
                return OrderType.TP
        return None

    @staticmethod
    def full_pos_id(signalId: str, direction: PositionDirection):
        if "-" in signalId:
            print("signal id must not include '-' but does: " + signalId)
        return signalId + "-" + str(direction.name)

    @staticmethod
    def split_pos_Id(posId: str):
        parts = posId.split("-")
        if len(parts) >= 2:
            if parts[1] == str(PositionDirection.SHORT.name):
                return [parts[0], PositionDirection.SHORT]
            elif parts[1] == str(PositionDirection.LONG.name):
                return [parts[0], PositionDirection.LONG]
        return [posId, None]

    @staticmethod
    def get_other_direction_id(posId: str):
        parts = TradingBot.split_pos_Id(posId)
        if parts[1] is not None:
            return TradingBot.full_pos_id(parts[0],
                                          PositionDirection.LONG if parts[1] == PositionDirection.SHORT
                                          else PositionDirection.SHORT)
        return None

    ############### handling of open orders

    def cancel_entry(self, positionId, account: Account):
        to_cancel = self.generate_order_id(positionId, OrderType.ENTRY)
        for o in account.open_orders:
            if o.id == to_cancel:
                self.order_interface.cancel_order(o)
                # only cancel position if entry was still there
                if positionId in self.open_positions.keys():
                    del self.open_positions[positionId]
                break

    def position_got_opened(self, position: Position, bars: List[Bar], account: Account):
        # empty hook for actual bot to maybe clear linked positions etc.
        pass

    def handle_opened_position(self, position: Position, order: Order, account: Account, bars: List[Bar]):
        position.status = "open"
        position.filled_entry = order.executed_price if order is not None else None
        position.entry_tstamp = order.execution_tstamp if order is not None else bars[0].tstamp

        self.position_got_opened(position, bars, account)

    def sync_executions(self, bars: List[Bar], account: Account):
        for order in account.order_history[self.known_order_history:]:
            if order.executed_amount == 0:
                continue
            posId = self.position_id_from_order_id(order.id)
            if posId not in self.open_positions.keys():
                self.logger.info("executed order not found in positions: " + order.id)
                continue
            position = self.open_positions[posId]

            if position is not None:
                orderType = self.order_type_from_order_id(order.id)
                if orderType == OrderType.ENTRY and (
                        position.status == "pending" or position.status == "triggered"):
                    self.logger.info("position %s got opened" % position.id)
                    self.handle_opened_position(position=position, order=order, account=account, bars=bars)

                elif (orderType == OrderType.SL or orderType == OrderType.TP) and position.status == "open":
                    self.logger.info("position %s got closed" % position.id)
                    position.status = "closed"
                    position.filled_exit = order.executed_price
                    position.exit_tstamp = order.execution_tstamp
                    position.exit_equity = account.equity
                    self.position_closed(position, account)
                else:
                    self.logger.warn(
                        "don't know what to do with execution of " + order.id + " for position " + str(
                            position))
            else:
                self.logger.warn("no position found on execution of " + order.id)

        self.known_order_history = len(account.order_history)
        self.sync_positions_with_open_orders(bars, account)

    def got_data_for_position_sync(self, bars: List[Bar]):
        raise NotImplementedError

    def get_stop_for_unmatched_amount(self, amount: float, bars: List[Bar]):
        return None

    def sync_positions_with_open_orders(self, bars: List[Bar], account: Account):
        open_pos = 0
        for pos in self.open_positions.values():
            if pos.status == "open":
                open_pos += pos.amount

        if not self.got_data_for_position_sync(bars):
            self.logger.warn("got no initial data, can't sync positions")
            return

        remaining_pos_ids = []
        remaining_pos_ids += self.open_positions.keys()
        remaining_orders = []
        remaining_orders += account.open_orders

        for order in account.open_orders:
            orderType = self.order_type_from_order_id(order.id)
            if orderType is None:
                remaining_orders.remove(order)
                continue  # none of ours
            posId = self.position_id_from_order_id(order.id)
            if posId in self.open_positions.keys():
                remaining_orders.remove(order)
                if posId in remaining_pos_ids:
                    remaining_pos_ids.remove(posId)

        if len(remaining_orders) == 0 and len(remaining_pos_ids) == 0 and abs(
                open_pos - account.open_position.quantity) < 0.1:
            return

        self.logger.info("Has to start order/pos sync with bot vs acc: %.1f vs. %.1f and %i vs %i, remaining: %i,  %i"
                         % (
                             open_pos, account.open_position.quantity, len(self.open_positions),
                             len(account.open_orders),
                             len(remaining_orders), len(remaining_pos_ids)))

        renamed_position_keys = []
        # now remaining orders and remaining positions contain the not matched ones
        for order in remaining_orders:
            orderType = self.order_type_from_order_id(order.id)
            posId = self.position_id_from_order_id(order.id)
            if orderType == OrderType.ENTRY:
                # check for a matching position
                matchedPos = False
                for tempPosId in remaining_pos_ids:
                    # no matching order found
                    pos = self.open_positions[tempPosId]
                    if (pos.status == "pending" or pos.status == "triggered") and pos.amount == order.amount and \
                            pos.wanted_entry == order.stop_price:
                        self.logger.info(
                            "unmatched open order %s matched to position %s. adapted pos Id" % (order.id, pos.id))
                        pos.id = posId  # this position now belongs to this order
                        if tempPosId in remaining_pos_ids:
                            remaining_pos_ids.remove(tempPosId)
                        matchedPos = True
                        break
                if not matchedPos:
                    # add position for unkown order
                    stop = self.get_stop_for_unmatched_amount(order.amount, bars)
                    if stop is not None:
                        newPos = Position(id=posId,
                                          entry=order.limit_price if order.limit_price is not None else order.stop_price,
                                          amount=order.amount,
                                          stop=stop,
                                          tstamp=bars[0].tstamp)
                        newPos.status = "pending" if not order.stop_triggered else "triggered"
                        self.open_positions[posId] = newPos
                        self.logger.warn("found unknown entry %s %.1f @ %.1f, added position"
                                         % (order.id, order.amount,
                                            order.stop_price if order.stop_price is not None else order.limit_price))
                    else:
                        self.logger.warn(
                            "found unknown entry %s %.1f @ %.1f, canceling"
                            % (order.id, order.amount,
                               order.stop_price if order.stop_price is not None else order.limit_price))
                        self.order_interface.cancel_order(order)

            if orderType in [OrderType.SL, OrderType.TP]:
                matchedPos = False
                if posId in renamed_position_keys:
                    # already adapted by order side of exit
                    continue

                for tempPosId in remaining_pos_ids:
                    pos = self.open_positions[tempPosId]
                    if (pos.status == "open") and pos.amount == -order.amount:
                        self.logger.info(
                            "unmatched exit order %s matched to position %s. adapted pos Id" % (order.id, pos.id))
                        pos.id = posId  # this position now belongs to this order
                        renamed_position_keys.append(pos.id)
                        if posId in remaining_pos_ids:
                            remaining_pos_ids.remove(tempPosId)
                        matchedPos = True
                        break
                # no matching order found
                if not matchedPos:
                    newPos = Position(id=posId, entry=None, amount=-order.amount,
                                      stop=order.stop_price, tstamp=bars[0].tstamp)
                    newPos.status = "open"
                    self.open_positions[posId] = newPos
                    renamed_position_keys.append(newPos.id)
                    self.logger.warn("found unknown exit %s %.1f @ %.1f" % (
                        order.id, order.amount,
                        order.stop_price if order.stop_price is not None else order.limit_price))

        # FIXME: need to check if all positions got exits in the market. inparity might mean that a position was closed but not updated
        self.logger.info("found " + str(len(self.open_positions)) + " existing positions on sync")

        remainingPosition = account.open_position.quantity
        for pos in self.open_positions.values():
            if pos.status == "open":
                remainingPosition -= pos.amount

        for posId in remaining_pos_ids:
            # no matching order found
            pos = self.open_positions[posId]
            if pos.status == "pending" or pos.status == "triggered":
                # should have the opening order in the system, but doesn't
                # not sure why: in doubt: not create wrong orders
                if remainingPosition * pos.amount > 0 and abs(remainingPosition) > abs(pos.amount):
                    # assume position was opened without us realizing (during downtime)
                    self.handle_opened_position(position=pos, order=None, bars=bars, account=account)
                    remainingPosition -= pos.amount
                else:
                    pos.status = "missed"
                    self.position_closed(pos, account)
            elif pos.status == "open":
                if remainingPosition * pos.amount > 0 and abs(remainingPosition) > abs(pos.amount):
                    self.order_interface.send_order(
                        Order(orderId=self.generate_order_id(posId, OrderType.SL), amount=-pos.amount,
                              stop=pos.initial_stop))
                    remainingPosition -= pos.amount
            else:
                self.position_closed(pos, account)

        if remainingPosition != 0:
            unmatched_stop = self.get_stop_for_unmatched_amount(remainingPosition, bars)
            signalId = str(bars[1].tstamp) + '+' + str(randint(0, 99))
            if unmatched_stop is not None:
                posId = self.full_pos_id(signalId,
                                         PositionDirection.LONG if remainingPosition > 0 else PositionDirection.SHORT)
                newPos = Position(id=posId, entry=None, amount=remainingPosition,
                                  stop=unmatched_stop, tstamp=bars[0].tstamp)
                newPos.status = "open"
                self.open_positions[posId] = newPos
                # add stop
                self.logger.info(
                    "couldn't account for " + str(newPos.amount) + " open contracts. Adding position with stop for it")
                self.order_interface.send_order(Order(orderId=self.generate_order_id(posId, OrderType.SL),
                                                      stop=newPos.initial_stop, amount=-newPos.amount))
            elif account.open_position.quantity * remainingPosition > 0:
                self.logger.info(
                    "couldn't account for " + str(remainingPosition) + " open contracts. Market close")
                self.order_interface.send_order(Order(orderId=signalId + "_marketClose", amount=-remainingPosition))
            else:
                self.logger.info(
                    "couldn't account for " + str(
                        remainingPosition) + " open contracts. But close would increase exposure-> ignored")

    #####################################################

    def save_open_positions(self):
        if self.unique_id is None:
            return
        base = 'openPositions/'
        try:
            os.makedirs(base)
        except Exception:
            pass
        with open(base + self._get_pos_file(), 'w') as file:
            pos_json = []
            for pos in self.open_positions:
                pos_json.append(self.open_positions[pos].to_json())
            data = {"last_time": self.last_time,
                    "last_tick": str(self.last_tick_time),
                    "positions": pos_json}
            json.dump(data, file)

    def cancel_all_orders_for_position(self, positionId, account: Account):
        to_cancel = []
        for order in account.open_orders:
            if self.position_id_from_order_id(order.id) == positionId:
                to_cancel.append(order)

        for o in to_cancel:
            self.order_interface.cancel_order(o)

    def position_closed(self, position: Position, account: Account):
        self.position_history.append(position)
        del self.open_positions[position.id]

        # cancel other open orders of this position (sl/tp etc)
        self.logger.info("canceling remaining orders for position: " + position.id)
        self.cancel_all_orders_for_position(position.id, account)

        if self.unique_id is None:
            return
        base = 'positionHistory/'
        filename = base + self._get_pos_file()
        size = 0
        try:
            os.makedirs(base)
        except Exception:
            pass
        try:
            size = os.path.getsize(filename)
        except Exception:
            pass
        with open(filename, 'a') as file:
            writer = csv.writer(file)
            if size == 0:
                csv_columns = ['signalTStamp', 'size', 'wantedEntry', 'initialStop', 'openTime', 'openPrice',
                               'closeTime',
                               'closePrice', 'equityOnExit']
                writer.writerow(csv_columns)
            writer.writerow([
                datetime.fromtimestamp(position.signal_tstamp).isoformat(),
                position.amount,
                position.wanted_entry,
                position.initial_stop,
                datetime.fromtimestamp(position.entry_tstamp).isoformat(),
                position.filled_entry,
                datetime.fromtimestamp(position.exit_tstamp).isoformat(),
                position.filled_exit,
                position.exit_equity
            ])

    def on_tick(self, bars: List[Bar], account: Account):
        """checks price and levels to manage current orders and set new ones"""
        self.last_tick_time = datetime.now()
        self.update_new_bar(bars)
        self.prep_bars(bars)
        try:
            self.manage_open_orders(bars, account)
            self.open_orders(bars, account)
        except Exception as e:
            self.save_open_positions()
            raise e
        self.save_open_positions()

    def prep_bars(self, bars: List[Bar]):
        pass

    ###
    # Order Management
    ###

    def manage_open_orders(self, bars: list, account: Account):
        pass

    def open_orders(self, bars: list, account: Account):
        pass

    def update_new_bar(self, bars: List[Bar]):
        """checks if this tick started a new bar.
        only works on the first call of a bar"""
        if bars[0].tstamp != self.last_time:
            self.last_time = bars[0].tstamp
            self.is_new_bar = True
        else:
            self.is_new_bar = False

    ####
    # additional stuff
    ###

    def create_performance_plot(self):
        self.logger.info("preparing stats")
        stats = {
            "dd": 0,
            "maxDD": 0,
            "hh": 1,
            "underwaterDays": 0,
            "percWin": 0,
            "avgResult": 0,
            "tradesInRange": 0,
            "maxWinner": 0,
            "maxLoser": 0
        }

        yaxis = {
            "equity": 'y1',
            "dd": 'y2',
            "maxDD": 'y2',
            "hh": 'y1',
            "underwaterDays": 'y5',
            "tradesInRange": 'y6',
            "percWin": 'y7',
            "avgResult": 'y4',
            "maxWinner": 'y4',
            "maxLoser": 'y4'
        }

        months_in_range = 1
        alpha = 0.3
        firstPos = self.position_history[0]
        lastHHTstamp = firstPos.signal_tstamp
        startEquity = firstPos.exit_equity - firstPos.amount * (1 / firstPos.filled_entry - 1 / firstPos.filled_exit)

        stats_range = []
        actual_history = list(
            filter(lambda p1: p1.filled_entry is not None and p1.filled_exit is not None, self.position_history))
        for pos in actual_history:
            # update range
            stats_range.append(pos)
            range_start = pos.exit_tstamp - months_in_range * 30 * 60 * 60 * 60
            while stats_range[0].exit_tstamp < range_start:
                stats_range.pop(0)

            avg = 0.0
            stats['tradesInRange'] = alpha * len(stats_range) + stats['tradesInRange'] * (1 - alpha)
            winners = 0.0
            maxWinner = 0
            maxLoser = 0
            for p in stats_range:
                # BEWARE: assumes inverse swap
                result = p.amount / p.filled_entry - p.amount / p.filled_exit
                maxLoser = min(result, maxLoser)
                maxWinner = max(result, maxWinner)
                avg += result / len(stats_range)
                if result > 0:
                    winners += 1.0

            stats['percWin'] = alpha * (100.0 * winners / len(stats_range)) + stats['percWin'] * (1 - alpha)
            stats['avgResult'] = alpha * avg + stats['avgResult'] * (1 - alpha)
            stats['maxWinner'] = alpha * maxWinner + stats['maxWinner'] * (1 - alpha)
            stats['maxLoser'] = alpha * (-maxLoser) + stats['maxLoser'] * (1 - alpha)

            if stats['hh'] < pos.exit_equity:
                stats['hh'] = pos.exit_equity
                lastHHTstamp = pos.exit_tstamp

            stats['underwaterDays'] = (pos.exit_tstamp - lastHHTstamp) / (60 * 60 * 24)
            dd = stats['hh'] - pos.exit_equity
            if dd > stats['maxDD']:
                stats['maxDD'] = dd
            stats['dd'] = dd
            stats['equity'] = pos.exit_equity

            pos.stats = stats.copy()
            pos.stats['equity'] = pos.exit_equity - startEquity
            pos.stats['hh'] = pos.stats['hh'] - startEquity

        self.logger.info("creating equityline")
        time = list(map(lambda p1: datetime.fromtimestamp(p1.exit_tstamp), actual_history))

        data = []
        for key in yaxis.keys():
            sub_data = list(map(lambda p1: p1.stats[key], actual_history))
            data.append(
                go.Scatter(x=time, y=sub_data, mode='lines', yaxis=yaxis[key], name=key + ":" + "%.1f" % (stats[key])))

        layout = go.Layout(
            xaxis=dict(
                anchor='y5'
            ),
            yaxis=dict(
                domain=[0.4, 1]
            ),
            yaxis2=dict(
                domain=[0.4, 1],
                range=[0, 2 * stats['maxDD']],
                overlaying='y',
                side='right'
            ),
            yaxis3=dict(
                domain=[0.2, 0.39]
            ),
            yaxis4=dict(
                domain=[0.2, 0.39],
                overlaying='y3',
                side='right'
            ),
            yaxis5=dict(
                domain=[0, 0.19]
            ),
            yaxis6=dict(
                domain=[0, 0.19],
                overlaying='y5',
                side='right'
            ),
            yaxis7=dict(
                domain=[0, 0.19],
                range=[0, 100],
                overlaying='y5',
                side='right'
            )
        )

        fig = go.Figure(data=data, layout=layout)
        fig.update_layout(xaxis_rangeslider_visible=False)
        return fig

    def add_to_plot(self, fig, bars, time):
        self.logger.info("adding trades")
        # trades
        for pos in self.position_history:
            if pos.status == "closed":
                fig.add_shape(go.layout.Shape(
                    type="line",
                    x0=datetime.fromtimestamp(pos.entry_tstamp),
                    y0=pos.filled_entry,
                    x1=datetime.fromtimestamp(pos.exit_tstamp),
                    y1=pos.filled_exit,
                    line=dict(
                        color="Green" if pos.amount > 0 else "Red",
                        width=2,
                        dash="solid"
                    )
                ))
            if pos.status == "notFilled":
                fig.add_shape(go.layout.Shape(
                    type="line",
                    x0=datetime.fromtimestamp(pos.signal_tstamp),
                    y0=pos.wanted_entry,
                    x1=datetime.fromtimestamp(pos.exit_tstamp),
                    y1=pos.wanted_entry,
                    line=dict(
                        color="Blue",
                        width=1,
                        dash="dot"
                    )
                ))

        fig.update_shapes(dict(xref='x', yref='y'))
