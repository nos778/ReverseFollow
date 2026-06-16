from typing import Literal

from pythongo.base import BaseParams, BaseState, BaseStrategy, Field
from pythongo.classdef import (
    CancelOrderData,
    InstrumentStatus,
    OrderData,
    TickData,
    TradeData,
)


class Params(BaseParams):
    """参数映射模型"""

    exchange: str = Field(default="", title="交易所代码")
    instrument_id: str = Field(default="", title="合约代码")
    source_investor: str = Field(default="", title="源账号")
    target_investor: str = Field(default="", title="目标账号")
    hedgeflag: Literal["1", "2", "3", "4", "5"] = Field(default="1", title="投机套保标志")
    reverse_ratio: Literal[0.5, 1.0, 1.5, 2.0, 3.0] = Field(default=1.0, title="反向系数")
    max_follow_volume: Literal[1, 2, 5, 10, 20, 50, 100] = Field(default=100, title="单次最大跟单手数")
    price_mode: Literal["opponent", "last"] = Field(default="opponent", title="发单价格模式")
    slippage_ticks: Literal[0, 1, 2, 3, 5, 10] = Field(default=0, title="滑点跳数")
    market: bool = Field(default=False, title="是否市价单")
    enable_trading: bool = Field(default=True, title="允许交易")


class State(BaseState):
    """状态映射模型"""

    source_long: int = Field(default=0, title="源多头")
    source_short: int = Field(default=0, title="源空头")
    target_long: int = Field(default=0, title="目标多头")
    target_short: int = Field(default=0, title="目标空头")
    expect_target_long: int = Field(default=0, title="理论目标多头")
    expect_target_short: int = Field(default=0, title="理论目标空头")
    last_order_id: int | None = Field(default=None, title="最后报单编号")
    last_action: str = Field(default="", title="最后动作")
    last_error: str = Field(default="", title="最后错误")
    has_pending_order: bool = Field(default=False, title="存在挂单")


class ReverseFollow(BaseStrategy):
    """同客户端多账号反向跟单策略"""

    def __init__(self) -> None:
        super().__init__()

        self.params_map = Params()
        self.state_map = State()

        self.pending_order_ids: set[int] = set()
        self.last_source_long = 0
        self.last_source_short = 0
        self.last_tick: TickData | None = None
        self.price_tick = 0.0

    def _is_own_order(self, order_id: int, memo: str) -> bool:
        memo_text = memo if isinstance(memo, str) else ""
        return order_id in self.pending_order_ids or memo_text.startswith("reverse_follow:")

    def _has_pending_order(self) -> bool:
        return bool(self.pending_order_ids)

    def _calc_expected_positions(self, source_long: int, source_short: int) -> tuple[int, int]:
        expect_target_long = int(source_short * self.params_map.reverse_ratio)
        expect_target_short = int(source_long * self.params_map.reverse_ratio)
        return expect_target_long, expect_target_short

    def _refresh_state(
        self,
        source_long: int | None = None,
        source_short: int | None = None,
        target_long: int | None = None,
        target_short: int | None = None,
        expect_target_long: int | None = None,
        expect_target_short: int | None = None,
    ) -> None:
        if source_long is not None:
            self.state_map.source_long = source_long
        if source_short is not None:
            self.state_map.source_short = source_short
        if target_long is not None:
            self.state_map.target_long = target_long
        if target_short is not None:
            self.state_map.target_short = target_short
        if expect_target_long is not None:
            self.state_map.expect_target_long = expect_target_long
        if expect_target_short is not None:
            self.state_map.expect_target_short = expect_target_short

        self.state_map.has_pending_order = self._has_pending_order()
        self.update_status_bar()

    def _extract_position_counts(self, position: object | None) -> tuple[int, int]:
        if position is None:
            return 0, 0

        long_position = getattr(getattr(position, "long", None), "position", 0)
        short_position = getattr(getattr(position, "short", None), "position", 0)
        return int(long_position), int(short_position)

    def _get_position_from_all(self, all_position: dict, investor: str) -> object | None:
        return (
            all_position.get(investor, {})
            .get(self.params_map.instrument_id, {})
            .get(self.params_map.hedgeflag)
        )

    def _get_position_by_fallback(self, investor: str) -> object | None:
        try:
            return self.get_position(
                instrument_id=self.params_map.instrument_id,
                hedgeflag=self.params_map.hedgeflag,
                investor=investor,
                simple=True,
            )
        except Exception as error:
            self.output(f"读取账号持仓失败 investor={investor}: {error}")
            return None

    def _read_positions(self) -> tuple[int, int, int, int]:
        source_position = None
        target_position = None
        all_position: dict | None = None

        get_all_position = getattr(self, "get_all_position", None)
        if callable(get_all_position):
            try:
                all_position = get_all_position(simple=True)
            except Exception as error:
                self.output(f"读取全部持仓失败，改用单账号查询: {error}")

        if isinstance(all_position, dict):
            source_position = self._get_position_from_all(all_position=all_position, investor=self.params_map.source_investor)
            target_position = self._get_position_from_all(all_position=all_position, investor=self.params_map.target_investor)

        if source_position is None:
            source_position = self._get_position_by_fallback(investor=self.params_map.source_investor)
        if target_position is None:
            target_position = self._get_position_by_fallback(investor=self.params_map.target_investor)

        if source_position is None:
            self.output(f"源账号持仓为空，按 0 处理 investor={self.params_map.source_investor}")
        if target_position is None:
            self.output(f"目标账号持仓为空，按 0 处理 investor={self.params_map.target_investor}")

        source_long, source_short = self._extract_position_counts(position=source_position)
        target_long, target_short = self._extract_position_counts(position=target_position)
        return source_long, source_short, target_long, target_short

    def _calc_order_price(self, order_direction: Literal["buy", "sell"]) -> float:
        if self.last_tick is None:
            self.output("没有最新 tick，跳过报单")
            return 0.0

        if self.params_map.price_mode == "opponent":
            if order_direction == "buy":
                price = self.last_tick.ask_price1
            else:
                price = self.last_tick.bid_price1
            if price <= 0:
                price = self.last_tick.last_price
        else:
            price = self.last_tick.last_price

        if self.price_tick > 0:
            slippage = self.params_map.slippage_ticks * self.price_tick
            if order_direction == "buy":
                price += slippage
            else:
                price -= slippage

        if price <= 0:
            self.output(
                f"报单价格无效 action_direction={order_direction} "
                f"last_price={self.last_tick.last_price}"
            )
            return 0.0

        return float(price)

    def _submit_order(
        self,
        action: str,
        volume: int,
        order_direction: Literal["buy", "sell"],
        is_close: bool,
    ) -> int | None:
        if self.last_tick is None:
            self.output(f"没有最新 tick，无法执行动作: {action}")
            return None

        if volume <= 0:
            return None

        price = self._calc_order_price(order_direction=order_direction)
        if price <= 0:
            return None

        memo = f"reverse_follow:{action}"

        if is_close:
            order_id = self.auto_close_position(
                exchange=self.params_map.exchange,
                instrument_id=self.params_map.instrument_id,
                volume=volume,
                price=price,
                order_direction=order_direction,
                investor=self.params_map.target_investor,
                hedgeflag=self.params_map.hedgeflag,
                market=self.params_map.market,
                memo=memo,
            )
        else:
            order_id = self.send_order(
                exchange=self.params_map.exchange,
                instrument_id=self.params_map.instrument_id,
                volume=volume,
                price=price,
                order_direction=order_direction,
                investor=self.params_map.target_investor,
                hedgeflag=self.params_map.hedgeflag,
                market=self.params_map.market,
                memo=memo,
            )

        if order_id in (None, -1):
            self.output(f"报单失败 action={action} volume={volume} price={price}")
            return order_id

        self.pending_order_ids.add(int(order_id))
        self.state_map.last_order_id = int(order_id)
        self.state_map.last_action = action
        self.state_map.has_pending_order = True
        self.output(
            f"发送委托 action={action} order_id={order_id} volume={volume} "
            f"direction={order_direction} price={price}"
        )
        self._refresh_state()
        return int(order_id)

    def on_init(self) -> None:
        super().on_init()

        self.load_instance_file()

        self.price_tick = 0.0
        if self.params_map.exchange and self.params_map.instrument_id:
            try:
                instrument_data = self.get_instrument_data(
                    exchange=self.params_map.exchange,
                    instrument_id=self.params_map.instrument_id,
                )
                if instrument_data is not None:
                    self.price_tick = float(getattr(instrument_data, "price_tick", 0.0))
            except Exception as error:
                self.price_tick = 0.0
                self.output(f"读取最小跳动价位失败: {error}")

        self._refresh_state()

    def on_start(self) -> None:
        super().on_start()

        self.pending_order_ids.clear()
        source_long, source_short, target_long, target_short = self._read_positions()
        self.last_source_long = source_long
        self.last_source_short = source_short
        expect_target_long, expect_target_short = self._calc_expected_positions(
            source_long=source_long,
            source_short=source_short,
        )
        self.output(
            "策略启动 "
            f"source={self.params_map.source_investor} "
            f"target={self.params_map.target_investor} "
            f"exchange={self.params_map.exchange} "
            f"instrument={self.params_map.instrument_id} "
            f"reverse_ratio={self.params_map.reverse_ratio}"
        )
        self._refresh_state(
            source_long=source_long,
            source_short=source_short,
            target_long=target_long,
            target_short=target_short,
            expect_target_long=expect_target_long,
            expect_target_short=expect_target_short,
        )

    def on_stop(self) -> None:
        super().on_stop()

        self.output(
            "策略停止 "
            f"source={self.params_map.source_investor} "
            f"target={self.params_map.target_investor} "
            f"instrument={self.params_map.instrument_id}"
        )
        self._refresh_state()

    def on_tick(self, tick: TickData) -> None:
        super().on_tick(tick)

        if tick.exchange != self.params_map.exchange or tick.instrument_id != self.params_map.instrument_id:
            return

        self.last_tick = tick
        source_long, source_short, target_long, target_short = self._read_positions()
        self.last_source_long = source_long
        self.last_source_short = source_short
        expect_target_long, expect_target_short = self._calc_expected_positions(
            source_long=source_long,
            source_short=source_short,
        )
        self._refresh_state(
            source_long=source_long,
            source_short=source_short,
            target_long=target_long,
            target_short=target_short,
            expect_target_long=expect_target_long,
            expect_target_short=expect_target_short,
        )

        if not self.params_map.enable_trading:
            self.output("当前实例未启用交易，仅更新状态")
            return

        if self._has_pending_order():
            return

        if target_long == expect_target_long and target_short == expect_target_short:
            return

        if target_long > expect_target_long:
            close_volume = min(target_long - expect_target_long, self.params_map.max_follow_volume)
            self._submit_order(
                action="close_long",
                volume=close_volume,
                order_direction="sell",
                is_close=True,
            )
            return

        if target_short > expect_target_short:
            close_volume = min(target_short - expect_target_short, self.params_map.max_follow_volume)
            self._submit_order(
                action="close_short",
                volume=close_volume,
                order_direction="buy",
                is_close=True,
            )
            return

        if target_long < expect_target_long:
            open_volume = min(expect_target_long - target_long, self.params_map.max_follow_volume)
            self._submit_order(
                action="open_long",
                volume=open_volume,
                order_direction="buy",
                is_close=False,
            )
            return

        if target_short < expect_target_short:
            open_volume = min(expect_target_short - target_short, self.params_map.max_follow_volume)
            self._submit_order(
                action="open_short",
                volume=open_volume,
                order_direction="sell",
                is_close=False,
            )
            return

    def on_contract_status(self, status: InstrumentStatus) -> None:
        super().on_contract_status(status)
        self.output(
            f"合约状态变化 exchange={status.exchange} "
            f"instrument={status.instrument_id} status={status.status}"
        )

    def on_order(self, order: OrderData) -> None:
        super().on_order(order)

        if not self._is_own_order(order_id=order.order_id, memo=order.memo):
            return

        remain_volume = order.total_volume - order.traded_volume - order.cancel_volume
        if remain_volume > 0:
            self.pending_order_ids.add(order.order_id)
        else:
            self.pending_order_ids.discard(order.order_id)

        self.state_map.last_order_id = order.order_id
        self.output(
            f"委托变化 order_id={order.order_id} status={order.status} "
            f"total={order.total_volume} traded={order.traded_volume} cancel={order.cancel_volume}"
        )
        self._refresh_state()

    def on_cancel(self, order: CancelOrderData) -> None:
        super().on_cancel(order)

        if not self._is_own_order(order_id=order.order_id, memo=order.memo):
            return

        self.pending_order_ids.discard(order.order_id)
        self.output(
            f"撤单回报 order_id={order.order_id} cancel_volume={order.cancel_volume}"
        )
        self._refresh_state()

    def on_order_trade(self, order: OrderData) -> None:
        super().on_order_trade(order)

        if not self._is_own_order(order_id=order.order_id, memo=order.memo):
            return

        self.output(
            f"报单成交回报 order_id={order.order_id} traded={order.traded_volume} "
            f"total={order.total_volume}"
        )

    def on_trade(self, trade: TradeData, log: bool = True) -> None:
        super().on_trade(trade, log=log)

        if not self._is_own_order(order_id=trade.order_id, memo=trade.memo):
            return

        self.output(
            f"成交回报 order_id={trade.order_id} trade_id={trade.trade_id} "
            f"direction={trade.direction} offset={trade.offset} volume={trade.volume} price={trade.price}"
        )

        source_long, source_short, target_long, target_short = self._read_positions()
        self.last_source_long = source_long
        self.last_source_short = source_short
        expect_target_long, expect_target_short = self._calc_expected_positions(
            source_long=source_long,
            source_short=source_short,
        )
        self._refresh_state(
            source_long=source_long,
            source_short=source_short,
            target_long=target_long,
            target_short=target_short,
            expect_target_long=expect_target_long,
            expect_target_short=expect_target_short,
        )

    def on_error(self, error: dict[str, str]) -> None:
        super().on_error(error)

        err_code = error.get("errCode", "")
        err_msg = error.get("errMsg", "")
        order_id_text = error.get("orderID", "")
        self.state_map.last_error = f"{err_code} {err_msg}".strip()

        if order_id_text:
            try:
                self.pending_order_ids.discard(int(order_id_text))
            except ValueError:
                self.output(f"错误回报中的 orderID 无法转换为整数: {order_id_text}")

        self.output(f"错误回报 errCode={err_code} errMsg={err_msg} orderID={order_id_text}")
        self._refresh_state()
