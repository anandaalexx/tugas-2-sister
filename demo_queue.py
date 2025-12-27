import asyncio
from src.nodes.queue_node import DistributedQueueNode

async def main():
    # Misal kita punya 3 node
    nodes = ["node-1", "node-2", "node-3"]

    # Inisialisasi tiga instance queue node
    node1 = DistributedQueueNode("node-1", nodes)
    node2 = DistributedQueueNode("node-2", nodes)
    node3 = DistributedQueueNode("node-3", nodes)

    # Connect ke Redis
    await node1.start()
    await node2.start()
    await node3.start()

    # PRODUCE pesan
    await node1.produce("orders", {"id": 1, "item": "apple"})
    await node2.produce("orders", {"id": 2, "item": "banana"})
    await node3.produce("orders", {"id": 3, "item": "cherry"})

    # CONSUME pesan di masing-masing node
    msg1 = await node1.consume("orders")
    msg2 = await node2.consume("orders")
    msg3 = await node3.consume("orders")

    print("Consumed:", msg1, msg2, msg3)

    # ACK pesan yang telah selesai diproses
    if msg1: await node1.ack("orders", msg1)
    if msg2: await node2.ack("orders", msg2)
    if msg3: await node3.ack("orders", msg3)

    # Simulasi recovery (pesan belum sempat di-ack)
    await node1.recover_unacked("orders")

if __name__ == "__main__":
    asyncio.run(main())
