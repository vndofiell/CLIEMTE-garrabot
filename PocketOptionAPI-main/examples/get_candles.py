from pocketoptionapi_async import AsyncPocketOptionClient

async def main():
    SSID = input("Enter your SSID: ")
    client = AsyncPocketOptionClient(SSID, is_demo=True, enable_logging=False)
    await client.connect()

    candles = await client.get_candles(asset='EURUSD_otc', timeframe=60)
    for candle in candles:
        print(f"open: {candle.open}, close: {candle.close}, high: {candle.high}, low: {candle.low}, volume: {candle.volume}, timestamp: {candle.timestamp}")

    await client.disconnect()

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())