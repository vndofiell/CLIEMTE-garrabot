from pocketoptionapi_async import AsyncPocketOptionClient

async def main():
    SSID = input("Enter your SSID: ")
    client = AsyncPocketOptionClient(SSID, is_demo=True, enable_logging=False)
    await client.connect()

    balance = await client.get_balance()
    print(f"Your balance is: {balance.balance}, currency: {balance.currency}")

    await client.disconnect()

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())