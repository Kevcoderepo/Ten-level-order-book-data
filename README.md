# Binance 十档深度行情终端

基于 WebSocket 实时展示币安交易对的十档买卖挂单数据，带有终端 TUI 界面。

![Python](https://img.shields.io/badge/Python-3.12+-blue)

## 功能

- 实时订阅币安 `depth10@100ms` 数据流
- 终端内以表格 + 柱状图展示买卖十档挂单
- 显示买卖价差（Spread）及百分比
- 支持运行时切换交易对（直接输入符号回车即可）
- 空格键暂停/恢复画面刷新
- F2 切换主题样式
- 自动重连（指数退避 + 随机抖动）
- 心跳检测，30 秒无数据自动重连
- 运行日志写入 `depth.log`

## 安装

```bash
pip install -r requirements.txt
```

依赖：`websockets`、`textual`

## 使用

```bash
# 默认查看 BTCUSDT
python main.py

# 指定交易对
python main.py ethusdt
```


## 项目结构

```
main.py         入口，解析命令行参数并启动 TUI
tui_app.py      Textual TUI 界面（深度表格、状态栏、输入框）
ws_manager.py   WebSocket 连接管理（订阅、重连、心跳、数据分发）
```

## 代码分析

- 点击链接跳转观看_youtube

[![](https://img.youtube.com/vi/CreprTgcyZM/hqdefault.jpg)](https://www.youtube.com/watch?v=CreprTgcyZM)
