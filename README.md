# Parataxis Family Bot

Internal Telegram bot I built for Parataxis Korea to keep our exec team updated on market news, stock prices, and mining operations without having to constantly check multiple sources manually.

## What it does

- Monitors DART (Korea's financial disclosure system) for filings from Parataxis Korea, Parataxis Ethereum, Bitmax, and Bitplanet and sends real-time alerts
- Tracks news for our portfolio companies and competitors (Strategy/MicroStrategy, Bitmax, Bitplanet) via Google News RSS
- Sends daily BTC/ETH price updates and KOSDAQ stock prices for PK (288330) and PE (290560)
- Pulls live Bitcoin mining stats from Luxor API (hashrate, active workers, BTC mined today/MTD)
- USD/KRW exchange rate via Korea Eximbank API
- Auto-translates Korean news titles to English using Anthropic's Claude API
- Logs everything to a Google Sheet watchlist for manual review
- Sends a daily HTML email digest at 10:05 KST to executives — only articles I manually approve get included
- Subscription manager so each user can toggle which alerts they want

## Why I built it

We had a contract with a Korean PR firm (KPR) that sent us daily news monitoring reports. I realized most of the infrastructure to replace it was already sitting in this bot, so I built an automated email digest to take over that function before the contract ended.

## Stack

Python 3.12 · python-telegram-bot · APScheduler · PostgreSQL · Google Sheets API · SendGrid · Anthropic API · Luxor API · DART API · Railway (deployment)
