"""
币安深度数据 WebSocket 管理器。

功能概述：
  - 订阅币安 depth10@100ms 流，获取指定交易对的前 10 档买卖挂单
  - 内置自动重连机制（指数退避 + 随机抖动），应对网络中断
  - 心跳检测：超过 30 秒无消息则主动断开并触发重连
  - 通过 on_data 回调将解析后的快照分发给 UI 层

典型用法：
    manager = BinanceDepthWSManager(symbol="btcusdt", on_data=callback)
    await manager.run_async()   # 在已有的 asyncio 事件循环中启动
"""

import asyncio
import json
import logging
import os
import random
import time
import urllib.request
from collections.abc import Callable
from enum import Enum, auto
from typing import Any

import websockets

# ── 日志初始化 ────────────────────────────────────────────────────
# 日志追加写入脚本同目录下的 depth.log
_script_dir = os.path.dirname(os.path.abspath(__file__))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    filename=os.path.join(_script_dir, "depth.log"),
    filemode="a", #a/w
)
logger = logging.getLogger(__name__)

# 2026-03-07 16:21:33 [INFO] Connected successfully
# 2026-03-07 16:21:58 [WARNING] Connection lost: ...

# ── 连接状态枚举 ──────────────────────────────────────────────────
class ConnectionState(Enum):
    """
    WebSocket 连接的四种状态，构成状态机：
    DISCONNECTED → CONNECTING → CONNECTED → (断线) → RECONNECTING → CONNECTING → ...
    """
    DISCONNECTED = auto()   # 初始状态 / 已断开
    CONNECTING = auto()     # 正在建立连接
    CONNECTED = auto()      # 连接成功，正常收发数据
    RECONNECTING = auto()   # 等待重连（退避延迟中）


class BinanceDepthWSManager:
    """
    币安 depth10 WebSocket 客户端。

    数据流：
        币安 WS → _listen() 接收原始 JSON
                → _on_message() 解析并打包为 snapshot dict
                → on_data 回调分发给上层（如 TUI DepthTable）

    可靠性：
        - _heartbeat_monitor() 每 5 秒检查一次最后消息时间，
          超过 HEARTBEAT_TIMEOUT 则主动关闭连接
        - _reconnect() 使用指数退避策略（1s → 2s → 4s ... 最大 60s）+ 随机抖动，
          避免大量客户端同时重连造成服务端压力
    """

    BASE_URL = "wss://stream.binance.com:9443/ws"
    BASE_DELAY = 1.0           # 指数退避重连基础延迟（秒），首次重连等待约 1 秒
    MAX_DELAY = 60.0           # 指数退避重连最大延迟（秒），无论重试多少次都不超过此值
    HEARTBEAT_TIMEOUT = 30.0   # 心跳超时阈值（秒），超时视为连接已死

    def __init__(
        self,
        symbol: str = "btcusdt",
        on_data: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        """
        Args:
            symbol:  交易对名称（如 "btcusdt"），会自动转小写
            on_data: 每次收到深度数据时的回调函数，参数为 snapshot dict，
                     包含 symbol / update_id / bids / asks / 统计信息
        """
        self._symbol = symbol.lower()
        self._on_data = on_data                              # UI 层数据回调
        self._state = ConnectionState.DISCONNECTED           # 当前连接状态
        self._ws: websockets.ClientConnection | None = None  # WebSocket 连接对象
        self._reconnect_attempts = 0                         # 本轮连续重连次数（成功后归零）
        self._last_msg_time = 0.0                            # 上次收到消息的时间戳
        self._total_messages = 0                             # 累计收到的消息总数
        self._total_reconnects = 0                           # 累计重连总次数
        self._start_time = 0.0                               # run_async 启动时的时间戳
        self._running = False                                # 运行标志，False 时各协程退出

    @property
    def _url(self) -> str:
        """拼接完整的 WebSocket 订阅地址。depth10 = 前 10 档，100ms = 推送间隔。"""
        return f"{self.BASE_URL}/{self._symbol}@depth10@100ms"

    # ── 状态机 ────────────────────────────────────────────────────
    def _set_state(self, new_state: ConnectionState) -> None:
        """切换连接状态并记录日志。"""
        old = self._state
        self._state = new_state
        logger.info("State: %s -> %s", old.name, new_state.name)

    # ── 异步入口 ──────────────────────────────────────────────────
    async def run_async(self) -> None:
        """
        异步入口，由外部事件循环驱动（如 Textual 的 run_worker）。
        调用后进入 _main 主循环，直到 shutdown() 被调用或任务被取消。
        """
        self._running = True
        self._start_time = time.time()
        await self._main()

    async def _main(self) -> None:
        """
        主循环：连接 → 并发运行（监听 + 心跳）→ 异常断线 → 重连。

        流程：
        1. _connect()         建立 WebSocket 连接
        2. asyncio.gather()   并发执行 _listen() 和 _heartbeat_monitor()
        3. 任一协程因连接断开抛出异常 → 捕获后进入 _reconnect()
        4. CancelledError     外部取消（如切换交易对）→ 直接退出循环
        """
        while self._running:
            try:
                await self._connect()
                await self._fetch_initial_snapshot()
                await asyncio.gather(
                    self._listen(),          # 接收并处理消息
                    self._heartbeat_monitor(),  # 监控心跳超时
                )
            except (
                websockets.ConnectionClosed,  # 服务端关闭连接或心跳检测主动关闭
                websockets.InvalidURI,        # URL 格式错误（交易对不存在等）
                OSError,                      # 网络不可达等系统级错误
            ) as exc:
                logger.warning("Connection lost: %s", exc)
            except asyncio.CancelledError:
                break  # 外部取消，干净退出

            if self._running:
                await self._reconnect()

    # ── 建立连接 ──────────────────────────────────────────────────
    async def _connect(self) -> None:
        """
        建立 WebSocket 连接。

        ping_interval=20: 每 20 秒发送一次 ping 帧保活
        ping_timeout=10:  10 秒内未收到 pong 则认为连接已死
        连接成功后重置连续重连计数器。
        """
        self._set_state(ConnectionState.CONNECTING)
        logger.info("Connecting to %s", self._url)
        self._ws = await websockets.connect(self._url, ping_interval=20, ping_timeout=10)
        self._set_state(ConnectionState.CONNECTED)
        self._reconnect_attempts = 0  # 连接成功，重置连续重连计数
        self._last_msg_time = time.time()
        logger.info("Connected successfully")

    # ── HTTP 初始快照 ────────────────────────────────────────────
    async def _fetch_initial_snapshot(self) -> None:
        """连接成功后立即通过 HTTP 拉取一次完整快照，消除等待首条 WS 推送的空窗。"""
        url = f"https://api.binance.com/api/v3/depth?symbol={self._symbol.upper()}&limit=10"
        try:
            loop = asyncio.get_running_loop()
            raw = await loop.run_in_executor(
                None, lambda: urllib.request.urlopen(url, timeout=5).read()
            )
            data = json.loads(raw)
            self._on_message(data)
            logger.info("Initial HTTP snapshot loaded")
        except Exception as exc:
            logger.warning("Failed to fetch initial snapshot: %s", exc)

    # ── 监听消息 ──────────────────────────────────────────────────
    async def _listen(self) -> None:
        """
        持续接收 WebSocket 消息。

        每收到一条消息：
        1. 更新 _last_msg_time（供心跳检测使用）
        2. 累加消息计数
        3. 解析 JSON 并调用 _on_message 分发
        """
        assert self._ws is not None
        async for raw in self._ws:
            if not self._running:
                break
            self._last_msg_time = time.time()
            self._total_messages += 1
            try:
                data = json.loads(raw)
                self._on_message(data)
            except json.JSONDecodeError:
                logger.warning("Invalid JSON: %s", raw[:200])

    # ── 心跳检测 ──────────────────────────────────────────────────
    async def _heartbeat_monitor(self) -> None:
        """
        每 5 秒检查一次是否在 HEARTBEAT_TIMEOUT 内收到过消息。

        如果超时（说明连接可能已静默断开），则主动关闭 WebSocket，
        这会让 _listen() 中的 async for 抛出 ConnectionClosed，
        从而触发 _main() 的异常处理 → 重连流程。
        """
        while self._running and self._state == ConnectionState.CONNECTED:
            await asyncio.sleep(5)
            elapsed = time.time() - self._last_msg_time
            if elapsed > self.HEARTBEAT_TIMEOUT:
                logger.warning("No message for %.1fs, closing connection", elapsed)
                if self._ws:
                    await self._ws.close()
                return

    # ── 指数退避重连 ──────────────────────────────────────────────
    async def _reconnect(self) -> None:
        """
        使用指数退避策略等待后重连。

        计算公式：wait = min(BASE_DELAY * 2^(attempt-1), MAX_DELAY) + random(0,1)
        示例：第 1 次 ≈1s, 第 2 次 ≈2s, 第 3 次 ≈4s, ..., 上限 60s
        随机抖动（jitter）避免多个客户端在同一时刻同时重连导致服务端压力。
        """
        if not self._running:
            return
        self._set_state(ConnectionState.RECONNECTING)
        self._total_reconnects += 1
        self._reconnect_attempts += 1
        delay = min(self.BASE_DELAY * (2 ** (self._reconnect_attempts - 1)), self.MAX_DELAY)
        wait = delay + random.uniform(0, 1)
        logger.info(
            "Reconnect #%d (total: %d) in %.1fs",
            self._reconnect_attempts,
            self._total_reconnects,
            wait,
        )
        await asyncio.sleep(wait)

    # ── 消息处理 ──────────────────────────────────────────────────
    def _on_message(self, data: dict) -> None:
        """
        将币安原始 JSON 打包为标准化的 snapshot dict，通过回调分发给 UI 层。

        snapshot 结构：
            {
                "symbol":           "BTCUSDT",
                "update_id":        8958175364,          # 币安的快照序列号
                "bids":             [["price","qty"]...], # 买单（出价从高到低）
                "asks":             [["price","qty"]...], # 卖单（要价从低到高）
                "total_messages":   1234,                 # 累计消息数
                "total_reconnects": 2,                    # 累计重连次数
                "uptime":           "01:23:45",           # 运行时长
                "state":            "CONNECTED",          # 当前连接状态
            }
        """
        snapshot = {
            "symbol": self._symbol.upper(),
            "update_id": data.get("lastUpdateId", "?"),
            "bids": data.get("bids", []),
            "asks": data.get("asks", []),
            "total_messages": self._total_messages,
            "total_reconnects": self._total_reconnects,
            "uptime": self._format_uptime(),
            "state": self._state.name,
        }
        if self._on_data:
            self._on_data(snapshot)

    # ── 关闭连接 ──────────────────────────────────────────────────
    def shutdown(self) -> None:
        """
        请求关闭：设置 _running=False 让各协程在下次检查时退出，
        并尝试异步关闭 WebSocket 连接。

        使用 get_running_loop().create_task() 而非 ensure_future()，
        在无事件循环时（如进程退出）静默忽略 RuntimeError。
        """
        logger.info("Shutdown requested")
        self._running = False
        if self._ws:
            try:
                asyncio.get_running_loop().create_task(self._ws.close())
            except RuntimeError:
                pass  # 无事件循环时静默忽略，连接会随进程结束自动断开

    # ── 工具方法 ──────────────────────────────────────────────────
    def _format_uptime(self) -> str:
        """将从启动到现在的秒数格式化为 HH:MM:SS 字符串。"""
        secs = int(time.time() - self._start_time)
        h, rem = divmod(secs, 3600)
        m, s = divmod(rem, 60)
        return f"{h:02d}:{m:02d}:{s:02d}"
