from pocketoptionapi_async import AsyncPocketOptionClient

async def main():
    SSID = input("Enter your SSID: ")
    client = AsyncPocketOptionClient(SSID, is_demo=True, enable_logging=False)
    await client.connect()

    stats = await client.get_connection_stats()
    print(f"Connection Stats: {stats}")

    await client.disconnect()

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())