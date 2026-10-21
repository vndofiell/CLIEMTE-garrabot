from pocketoptionapi_async import AsyncPocketOptionClient

async def main():
    SSID = input("Enter your SSID: ")
    client = AsyncPocketOptionClient(SSID, is_demo=True, enable_logging=False)
    await client.connect()

    candles_df = await client.get_candles_dataframe(asset='EURUSD_otc', timeframe=60)
    print(candles_df)
    await client.disconnect()

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())