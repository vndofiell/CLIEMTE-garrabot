from pocketoptionapi_async import AsyncPocketOptionClient, OrderDirection

async def main():
    SSID = input("Enter your SSID: ")
    client = AsyncPocketOptionClient(SSID, is_demo=True, enable_logging=False)
    await client.connect()

    # Example of placing a call order
    try:
        amount = float(input("Enter the amount to invest: "))
        symbol = input("Enter the symbol (e.g., 'EURUSD_otc'): ")
        direction = OrderDirection.PUT

        order = await client.place_order(asset=symbol, amount=amount, direction=direction, duration=5)
        result = await client.check_order_result(order.order_id)
        if result:
            print(f"Order placed successfully: {result}")
        else:
            print("Order result is None, please check the order status manually.")
    except Exception as e:
        print(f"An error occurred while placing the order: {e}")

    await client.disconnect()

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())