import asyncio
import contextlib
import copy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Tuple

import pytest
import pytest_asyncio

from chia.consensus.block_rewards import calculate_base_farmer_reward, calculate_pool_reward
from chia.data_layer.data_layer import DataLayer
from chia.data_layer.data_layer_errors import OfferIntegrityError
from chia.data_layer.data_layer_util import OfferStore, StoreProofs
from chia.data_layer.data_layer_wallet import DataLayerWallet, verify_offer
from chia.rpc.data_layer_rpc_api import DataLayerRpcApi
from chia.rpc.rpc_server import start_rpc_server
from chia.rpc.wallet_rpc_api import WalletRpcApi
from chia.server.start_data_layer import create_data_layer_service
from chia.simulator.block_tools import BlockTools
from chia.simulator.full_node_simulator import FullNodeSimulator, backoff_times
from chia.simulator.simulator_protocol import FarmNewBlockProtocol
from chia.simulator.time_out_assert import time_out_assert
from chia.types.blockchain_format.sized_bytes import bytes32
from chia.types.peer_info import PeerInfo
from chia.util.byte_types import hexstr_to_bytes
from chia.util.config import save_config
from chia.util.ints import uint16, uint32
from chia.wallet.trading.offer import Offer as TradingOffer
from chia.wallet.wallet import Wallet
from chia.wallet.wallet_node import WalletNode
from tests.setup_nodes import setup_simulators_and_wallets
from tests.wallet.rl_wallet.test_rl_rpc import is_transaction_confirmed

pytestmark = pytest.mark.data_layer
nodes = Tuple[WalletNode, FullNodeSimulator]
nodes_with_port = Tuple[WalletNode, FullNodeSimulator, int, BlockTools]
wallet_and_port_tuple = Tuple[WalletNode, int]
two_wallets_with_port = Tuple[Tuple[wallet_and_port_tuple, wallet_and_port_tuple], FullNodeSimulator, BlockTools]


@contextlib.asynccontextmanager
async def init_data_layer(wallet_rpc_port: int, bt: BlockTools, db_path: Path) -> AsyncIterator[DataLayer]:
    config = bt.config
    config["data_layer"]["wallet_peer"]["port"] = wallet_rpc_port
    # TODO: running the data server causes the RPC tests to hang at the end
    config["data_layer"]["run_server"] = False
    config["data_layer"]["port"] = 0
    config["data_layer"]["rpc_port"] = 0
    config["data_layer"]["database_path"] = str(db_path.joinpath("db.sqlite"))
    save_config(bt.root_path, "config.yaml", config)
    service = create_data_layer_service(root_path=bt.root_path, config=config)
    await service.start()
    try:
        yield service._api.data_layer
    finally:
        service.stop()
        await service.wait_closed()


@pytest_asyncio.fixture(scope="function")
async def one_wallet_node_and_rpc() -> AsyncIterator[nodes_with_port]:
    async for nodes in setup_simulators_and_wallets(simulator_count=1, wallet_count=1, dic={}):
        full_nodes, wallets, bt = nodes
        [[wallet_node_0, wallet_server_0]] = wallets
        config = bt.config
        hostname = config["self_hostname"]
        daemon_port = config["daemon_port"]
        rpc_cleanup, rpc_port = await start_rpc_server(
            WalletRpcApi(wallet_node_0),
            hostname,
            daemon_port,
            wallet_node_0.server._port,
            lambda: None,
            bt.root_path,
            config,
            connect_to_daemon=False,
        )
        yield wallet_node_0, full_nodes[0], rpc_port, bt
        await rpc_cleanup()


@pytest_asyncio.fixture(scope="function")
async def two_wallet_node_and_rpc() -> AsyncIterator[two_wallets_with_port]:
    async for nodes in setup_simulators_and_wallets(simulator_count=1, wallet_count=2, dic={}):
        full_nodes, wallets, bt = nodes
        [full_node] = full_nodes
        [[wallet_node_0, wallet_server_0], [wallet_node_1, wallet_server_1]] = wallets
        config = bt.config
        hostname = config["self_hostname"]
        daemon_port = config["daemon_port"]
        rpc_cleanup_0, rpc_port_0 = await start_rpc_server(
            WalletRpcApi(wallet_node_0),
            hostname,
            daemon_port,
            wallet_node_0.server._port,
            lambda: None,
            bt.root_path,
            config,
            connect_to_daemon=False,
        )
        rpc_cleanup_1, rpc_port_1 = await start_rpc_server(
            WalletRpcApi(wallet_node_1),
            hostname,
            daemon_port,
            wallet_node_1.server._port,
            lambda: None,
            bt.root_path,
            config,
            connect_to_daemon=False,
        )
        yield ((wallet_node_0, rpc_port_0), (wallet_node_1, rpc_port_1)), full_node, bt
        await rpc_cleanup_1()
        await rpc_cleanup_0()


@pytest_asyncio.fixture(name="bare_data_layer_api")
async def bare_data_layer_api_fixture(tmp_path: Path, bt: BlockTools) -> AsyncIterator[DataLayerRpcApi]:
    # we won't use this port, this fixture is for _just_ a data layer rpc
    port = 1
    async with init_data_layer(wallet_rpc_port=port, bt=bt, db_path=tmp_path.joinpath(str(port))) as data_layer:
        data_rpc_api = DataLayerRpcApi(data_layer)
        yield data_rpc_api


@pytest.mark.asyncio
async def test_create_insert_get(one_wallet_node_and_rpc: nodes_with_port, tmp_path: Path) -> None:
    wallet_node, full_node_api, wallet_rpc_port, bt = one_wallet_node_and_rpc
    num_blocks = 15
    assert wallet_node.server
    await wallet_node.server.start_client(PeerInfo("localhost", uint16(full_node_api.server._port)), None)
    ph = await wallet_node.wallet_state_manager.main_wallet.get_new_puzzlehash()
    for i in range(0, num_blocks):
        await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(ph))
        await asyncio.sleep(0.5)
    funds = sum(
        [calculate_pool_reward(uint32(i)) + calculate_base_farmer_reward(uint32(i)) for i in range(1, num_blocks)]
    )
    await time_out_assert(15, wallet_node.wallet_state_manager.main_wallet.get_confirmed_balance, funds)
    wallet_rpc_api = WalletRpcApi(wallet_node)
    async with init_data_layer(wallet_rpc_port=wallet_rpc_port, bt=bt, db_path=tmp_path) as data_layer:
        # test insert
        data_rpc_api = DataLayerRpcApi(data_layer)
        key = b"a"
        value = b"\x00\x01"
        changelist: List[Dict[str, str]] = [{"action": "insert", "key": key.hex(), "value": value.hex()}]
        res = await data_rpc_api.create_data_store({})
        assert res is not None
        store_id = bytes32(hexstr_to_bytes(res["id"]))
        for i in range(0, num_blocks):
            await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(ph))
            await asyncio.sleep(0.2)
        res = await data_rpc_api.batch_update({"id": store_id.hex(), "changelist": changelist})
        update_tx_rec0 = res["tx_id"]
        await asyncio.sleep(1)
        for i in range(0, num_blocks):
            await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(ph))
            await asyncio.sleep(0.2)
        await time_out_assert(15, is_transaction_confirmed, True, "this is unused", wallet_rpc_api, update_tx_rec0)
        res = await data_rpc_api.get_value({"id": store_id.hex(), "key": key.hex()})
        wallet_root = await data_rpc_api.get_root({"id": store_id.hex()})
        local_root = await data_rpc_api.get_local_root({"id": store_id.hex()})
        assert wallet_root["hash"] == local_root["hash"]
        assert hexstr_to_bytes(res["value"]) == value

        # test delete unknown key
        unknown_key = b"b"
        changelist = [{"action": "delete", "key": unknown_key.hex()}]
        with pytest.raises(ValueError, match="Changelist resulted in no change to tree data"):
            await data_rpc_api.batch_update({"id": store_id.hex(), "changelist": changelist})

        # test delete
        changelist = [{"action": "delete", "key": key.hex()}]
        res = await data_rpc_api.batch_update({"id": store_id.hex(), "changelist": changelist})
        update_tx_rec1 = res["tx_id"]
        await asyncio.sleep(1)
        for i in range(0, num_blocks):
            await asyncio.sleep(1)
            await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(ph))
        await time_out_assert(15, is_transaction_confirmed, True, "this is unused", wallet_rpc_api, update_tx_rec1)
        with pytest.raises(Exception):
            await data_rpc_api.get_value({"id": store_id.hex(), "key": key.hex()})
        wallet_root = await data_rpc_api.get_root({"id": store_id.hex()})
        local_root = await data_rpc_api.get_local_root({"id": store_id.hex()})
        assert wallet_root["hash"] == bytes32([0] * 32)
        assert local_root["hash"] is None

        # test empty changelist
        changelist = []
        with pytest.raises(ValueError, match="Changelist resulted in no change to tree data"):
            await data_rpc_api.batch_update({"id": store_id.hex(), "changelist": changelist})


@pytest.mark.asyncio
async def test_upsert(one_wallet_node_and_rpc: nodes_with_port, tmp_path: Path) -> None:
    wallet_node, full_node_api, wallet_rpc_port, bt = one_wallet_node_and_rpc
    num_blocks = 15
    assert wallet_node.server
    await wallet_node.server.start_client(PeerInfo("localhost", uint16(full_node_api.server._port)), None)
    ph = await wallet_node.wallet_state_manager.main_wallet.get_new_puzzlehash()
    for i in range(0, num_blocks):
        await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(ph))
        await asyncio.sleep(0.5)
    funds = sum(
        [calculate_pool_reward(uint32(i)) + calculate_base_farmer_reward(uint32(i)) for i in range(1, num_blocks)]
    )
    await time_out_assert(15, wallet_node.wallet_state_manager.main_wallet.get_confirmed_balance, funds)
    wallet_rpc_api = WalletRpcApi(wallet_node)
    async with init_data_layer(wallet_rpc_port=wallet_rpc_port, bt=bt, db_path=tmp_path) as data_layer:
        # test insert
        data_rpc_api = DataLayerRpcApi(data_layer)
        key = b"a"
        value = b"\x00\x01"
        changelist: List[Dict[str, str]] = [
            {"action": "delete", "key": key.hex()},
            {"action": "insert", "key": key.hex(), "value": value.hex()},
        ]
        res = await data_rpc_api.create_data_store({})
        assert res is not None
        store_id = bytes32.from_hexstr(res["id"])
        for i in range(0, num_blocks):
            await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(ph))
            await asyncio.sleep(0.2)
        res = await data_rpc_api.batch_update({"id": store_id.hex(), "changelist": changelist})
        update_tx_rec0 = res["tx_id"]
        await asyncio.sleep(1)
        for i in range(0, num_blocks):
            await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(ph))
            await asyncio.sleep(0.2)
        await time_out_assert(15, is_transaction_confirmed, True, "this is unused", wallet_rpc_api, update_tx_rec0)
        res = await data_rpc_api.get_value({"id": store_id.hex(), "key": key.hex()})
        wallet_root = await data_rpc_api.get_root({"id": store_id.hex()})
        local_root = await data_rpc_api.get_local_root({"id": store_id.hex()})
        assert wallet_root["hash"] == local_root["hash"]
        assert hexstr_to_bytes(res["value"]) == value


@pytest.mark.asyncio
async def test_create_double_insert(one_wallet_node_and_rpc: nodes_with_port, tmp_path: Path) -> None:
    wallet_node, full_node_api, wallet_rpc_port, bt = one_wallet_node_and_rpc
    num_blocks = 15
    assert wallet_node.server
    await wallet_node.server.start_client(PeerInfo("localhost", uint16(full_node_api.server._port)), None)
    ph = await wallet_node.wallet_state_manager.main_wallet.get_new_puzzlehash()
    for i in range(0, num_blocks):
        await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(ph))
        await asyncio.sleep(0.5)
    funds = sum(
        [calculate_pool_reward(uint32(i)) + calculate_base_farmer_reward(uint32(i)) for i in range(1, num_blocks)]
    )
    await time_out_assert(15, wallet_node.wallet_state_manager.main_wallet.get_confirmed_balance, funds)
    wallet_rpc_api = WalletRpcApi(wallet_node)
    async with init_data_layer(wallet_rpc_port=wallet_rpc_port, bt=bt, db_path=tmp_path) as data_layer:
        data_rpc_api = DataLayerRpcApi(data_layer)
        res = await data_rpc_api.create_data_store({})
        assert res is not None
        store_id = bytes32(hexstr_to_bytes(res["id"]))
        for i in range(0, num_blocks):
            await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(ph))
            await asyncio.sleep(0.2)

        key1 = b"a"
        value1 = b"\x01\x02"
        changelist: List[Dict[str, str]] = [{"action": "insert", "key": key1.hex(), "value": value1.hex()}]
        res = await data_rpc_api.batch_update({"id": store_id.hex(), "changelist": changelist})
        update_tx_rec0 = res["tx_id"]
        await asyncio.sleep(1)
        for i in range(0, num_blocks):
            await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(ph))
            await asyncio.sleep(0.2)
        await time_out_assert(15, is_transaction_confirmed, True, "this is unused", wallet_rpc_api, update_tx_rec0)
        res = await data_rpc_api.get_value({"id": store_id.hex(), "key": key1.hex()})
        assert hexstr_to_bytes(res["value"]) == value1
        key2 = b"b"
        value2 = b"\x01\x23"
        changelist = [{"action": "insert", "key": key2.hex(), "value": value2.hex()}]
        res = await data_rpc_api.batch_update({"id": store_id.hex(), "changelist": changelist})
        update_tx_rec0 = res["tx_id"]
        await asyncio.sleep(1)
        for i in range(0, num_blocks):
            await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(ph))
            await asyncio.sleep(0.2)
        await time_out_assert(15, is_transaction_confirmed, True, "this is unused", wallet_rpc_api, update_tx_rec0)
        res = await data_rpc_api.get_value({"id": store_id.hex(), "key": key2.hex()})
        assert hexstr_to_bytes(res["value"]) == value2
        changelist = [{"action": "delete", "key": key1.hex()}]
        res = await data_rpc_api.batch_update({"id": store_id.hex(), "changelist": changelist})
        update_tx_rec1 = res["tx_id"]
        await asyncio.sleep(1)
        for i in range(0, num_blocks):
            await asyncio.sleep(1)
            await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(ph))
        await time_out_assert(15, is_transaction_confirmed, True, "this is unused", wallet_rpc_api, update_tx_rec1)
        with pytest.raises(Exception):
            await data_rpc_api.get_value({"id": store_id.hex(), "key": key1.hex()})


@pytest.mark.asyncio
async def test_keys_values_ancestors(one_wallet_node_and_rpc: nodes_with_port, tmp_path: Path) -> None:
    wallet_node, full_node_api, wallet_rpc_port, bt = one_wallet_node_and_rpc
    num_blocks = 15
    assert wallet_node.server
    await wallet_node.server.start_client(PeerInfo("localhost", uint16(full_node_api.server._port)), None)
    ph = await wallet_node.wallet_state_manager.main_wallet.get_new_puzzlehash()
    for i in range(0, num_blocks):
        await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(ph))
        await asyncio.sleep(0.5)
    funds = sum(
        [calculate_pool_reward(uint32(i)) + calculate_base_farmer_reward(uint32(i)) for i in range(1, num_blocks)]
    )
    await time_out_assert(15, wallet_node.wallet_state_manager.main_wallet.get_confirmed_balance, funds)
    wallet_rpc_api = WalletRpcApi(wallet_node)
    # TODO: with this being a pseudo context manager'ish thing it doesn't actually handle shutdown
    async with init_data_layer(wallet_rpc_port=wallet_rpc_port, bt=bt, db_path=tmp_path) as data_layer:
        data_rpc_api = DataLayerRpcApi(data_layer)
        res = await data_rpc_api.create_data_store({})
        assert res is not None
        store_id = bytes32(hexstr_to_bytes(res["id"]))
        for i in range(0, num_blocks):
            await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(ph))
            await asyncio.sleep(0.2)
        key1 = b"a"
        value1 = b"\x01\x02"
        changelist: List[Dict[str, str]] = [{"action": "insert", "key": key1.hex(), "value": value1.hex()}]
        key2 = b"b"
        value2 = b"\x03\x02"
        changelist.append({"action": "insert", "key": key2.hex(), "value": value2.hex()})
        key3 = b"c"
        value3 = b"\x04\x05"
        changelist.append({"action": "insert", "key": key3.hex(), "value": value3.hex()})
        key4 = b"d"
        value4 = b"\x06\x03"
        changelist.append({"action": "insert", "key": key4.hex(), "value": value4.hex()})
        key5 = b"e"
        value5 = b"\x07\x01"
        changelist.append({"action": "insert", "key": key5.hex(), "value": value5.hex()})
        res = await data_rpc_api.batch_update({"id": store_id.hex(), "changelist": changelist})
        update_tx_rec0 = res["tx_id"]
        await asyncio.sleep(1)
        for i in range(0, num_blocks):
            await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(ph))
            await asyncio.sleep(0.2)
        await time_out_assert(15, is_transaction_confirmed, True, "this is unused", wallet_rpc_api, update_tx_rec0)
        val = await data_rpc_api.get_keys_values({"id": store_id.hex()})
        keys = await data_rpc_api.get_keys({"id": store_id.hex()})
        dic = {}
        for item in val["keys_values"]:
            dic[item["key"]] = item["value"]
        assert dic["0x" + key1.hex()] == "0x" + value1.hex()
        assert dic["0x" + key2.hex()] == "0x" + value2.hex()
        assert dic["0x" + key3.hex()] == "0x" + value3.hex()
        assert dic["0x" + key4.hex()] == "0x" + value4.hex()
        assert dic["0x" + key5.hex()] == "0x" + value5.hex()
        assert len(keys["keys"]) == len(dic)
        for key in keys["keys"]:
            assert key in dic
        val = await data_rpc_api.get_ancestors({"id": store_id.hex(), "hash": val["keys_values"][4]["hash"]})
        # todo better assertions for get_ancestors result
        assert len(val["ancestors"]) == 3
        res_before = await data_rpc_api.get_root({"id": store_id.hex()})
        assert res_before["confirmed"] is True
        assert res_before["timestamp"] > 0
        key6 = b"tasdfsd"
        value6 = b"\x08\x02"
        changelist = [{"action": "insert", "key": key6.hex(), "value": value6.hex()}]
        key7 = b"basdff"
        value7 = b"\x09\x02"
        changelist.append({"action": "insert", "key": key7.hex(), "value": value7.hex()})
        res = await data_rpc_api.batch_update({"id": store_id.hex(), "changelist": changelist})
        update_tx_rec0 = res["tx_id"]
        await asyncio.sleep(1)
        for i in range(0, num_blocks):
            await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(ph))
            await asyncio.sleep(0.2)
        await time_out_assert(15, is_transaction_confirmed, True, "this is unused", wallet_rpc_api, update_tx_rec0)
        res_after = await data_rpc_api.get_root({"id": store_id.hex()})
        assert res_after["confirmed"] is True
        assert res_after["timestamp"] > res_before["timestamp"]
        pairs_before = await data_rpc_api.get_keys_values({"id": store_id.hex(), "root_hash": res_before["hash"].hex()})
        pairs_after = await data_rpc_api.get_keys_values({"id": store_id.hex(), "root_hash": res_after["hash"].hex()})
        keys_before = await data_rpc_api.get_keys({"id": store_id.hex(), "root_hash": res_before["hash"].hex()})
        keys_after = await data_rpc_api.get_keys({"id": store_id.hex(), "root_hash": res_after["hash"].hex()})
        assert len(pairs_before["keys_values"]) == len(keys_before["keys"]) == 5
        assert len(pairs_after["keys_values"]) == len(keys_after["keys"]) == 7


@pytest.mark.asyncio
async def test_get_roots(one_wallet_node_and_rpc: nodes_with_port, tmp_path: Path) -> None:
    wallet_node, full_node_api, wallet_rpc_port, bt = one_wallet_node_and_rpc
    num_blocks = 15
    assert wallet_node.server
    await wallet_node.server.start_client(PeerInfo("localhost", uint16(full_node_api.server._port)), None)
    ph = await wallet_node.wallet_state_manager.main_wallet.get_new_puzzlehash()
    for i in range(0, num_blocks):
        await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(ph))
        await asyncio.sleep(0.5)
    funds = sum(
        [calculate_pool_reward(uint32(i)) + calculate_base_farmer_reward(uint32(i)) for i in range(1, num_blocks)]
    )
    await time_out_assert(15, wallet_node.wallet_state_manager.main_wallet.get_confirmed_balance, funds)
    wallet_rpc_api = WalletRpcApi(wallet_node)
    async with init_data_layer(wallet_rpc_port=wallet_rpc_port, bt=bt, db_path=tmp_path) as data_layer:
        data_rpc_api = DataLayerRpcApi(data_layer)
        res = await data_rpc_api.create_data_store({})
        assert res is not None
        store_id1 = bytes32(hexstr_to_bytes(res["id"]))
        for i in range(0, num_blocks):
            await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(ph))
            await asyncio.sleep(0.2)

        res = await data_rpc_api.create_data_store({})
        assert res is not None
        store_id2 = bytes32(hexstr_to_bytes(res["id"]))
        for i in range(0, num_blocks):
            await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(ph))
            await asyncio.sleep(0.2)

        key1 = b"a"
        value1 = b"\x01\x02"
        changelist: List[Dict[str, str]] = [{"action": "insert", "key": key1.hex(), "value": value1.hex()}]
        key2 = b"b"
        value2 = b"\x03\x02"
        changelist.append({"action": "insert", "key": key2.hex(), "value": value2.hex()})
        key3 = b"c"
        value3 = b"\x04\x05"
        changelist.append({"action": "insert", "key": key3.hex(), "value": value3.hex()})
        res = await data_rpc_api.batch_update({"id": store_id1.hex(), "changelist": changelist})
        update_tx_rec0 = res["tx_id"]
        await asyncio.sleep(1)
        for i in range(0, num_blocks):
            await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(ph))
            await asyncio.sleep(0.2)
        await time_out_assert(15, is_transaction_confirmed, True, "this is unused", wallet_rpc_api, update_tx_rec0)
        roots = await data_rpc_api.get_roots({"ids": [store_id1.hex(), store_id2.hex()]})
        assert roots["root_hashes"][1]["id"] == store_id2
        assert roots["root_hashes"][1]["hash"] == bytes32([0] * 32)
        assert roots["root_hashes"][1]["confirmed"] is True
        assert roots["root_hashes"][1]["timestamp"] > 0
        key4 = b"d"
        value4 = b"\x06\x03"
        changelist = [{"action": "insert", "key": key4.hex(), "value": value4.hex()}]
        key5 = b"e"
        value5 = b"\x07\x01"
        changelist.append({"action": "insert", "key": key5.hex(), "value": value5.hex()})
        res = await data_rpc_api.batch_update({"id": store_id2.hex(), "changelist": changelist})
        update_tx_rec1 = res["tx_id"]
        await asyncio.sleep(1)
        for i in range(0, num_blocks):
            await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(ph))
            await asyncio.sleep(0.2)
        await time_out_assert(15, is_transaction_confirmed, True, "this is unused", wallet_rpc_api, update_tx_rec1)
        roots = await data_rpc_api.get_roots({"ids": [store_id1.hex(), store_id2.hex()]})
        assert roots["root_hashes"][1]["id"] == store_id2
        assert roots["root_hashes"][1]["hash"] is not None
        assert roots["root_hashes"][1]["hash"] != bytes32([0] * 32)
        assert roots["root_hashes"][1]["confirmed"] is True
        assert roots["root_hashes"][1]["timestamp"] > 0


@pytest.mark.asyncio
async def test_get_root_history(one_wallet_node_and_rpc: nodes_with_port, tmp_path: Path) -> None:
    wallet_node, full_node_api, wallet_rpc_port, bt = one_wallet_node_and_rpc
    num_blocks = 15
    assert wallet_node.server
    await wallet_node.server.start_client(PeerInfo("localhost", uint16(full_node_api.server._port)), None)
    ph = await wallet_node.wallet_state_manager.main_wallet.get_new_puzzlehash()
    for i in range(0, num_blocks):
        await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(ph))
        await asyncio.sleep(0.5)
    funds = sum(
        [calculate_pool_reward(uint32(i)) + calculate_base_farmer_reward(uint32(i)) for i in range(1, num_blocks)]
    )
    await time_out_assert(15, wallet_node.wallet_state_manager.main_wallet.get_confirmed_balance, funds)
    wallet_rpc_api = WalletRpcApi(wallet_node)
    async with init_data_layer(wallet_rpc_port=wallet_rpc_port, bt=bt, db_path=tmp_path) as data_layer:
        data_rpc_api = DataLayerRpcApi(data_layer)
        res = await data_rpc_api.create_data_store({})
        assert res is not None
        store_id1 = bytes32(hexstr_to_bytes(res["id"]))
        for i in range(0, num_blocks):
            await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(ph))
            await asyncio.sleep(0.2)

        res = await data_rpc_api.create_data_store({})
        assert res is not None

        key1 = b"a"
        value1 = b"\x01\x02"
        changelist: List[Dict[str, str]] = [{"action": "insert", "key": key1.hex(), "value": value1.hex()}]
        key2 = b"b"
        value2 = b"\x03\x02"
        changelist.append({"action": "insert", "key": key2.hex(), "value": value2.hex()})
        key3 = b"c"
        value3 = b"\x04\x05"
        changelist.append({"action": "insert", "key": key3.hex(), "value": value3.hex()})
        res = await data_rpc_api.batch_update({"id": store_id1.hex(), "changelist": changelist})
        update_tx_rec0 = res["tx_id"]
        await asyncio.sleep(1)
        for i in range(0, num_blocks):
            await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(ph))
            await asyncio.sleep(0.2)
        await time_out_assert(15, is_transaction_confirmed, True, "this is unused", wallet_rpc_api, update_tx_rec0)
        history1 = await data_rpc_api.get_root_history({"id": store_id1.hex()})
        assert len(history1["root_history"]) == 2
        assert history1["root_history"][0]["root_hash"] == bytes32([0] * 32)
        assert history1["root_history"][0]["confirmed"] is True
        assert history1["root_history"][0]["timestamp"] > 0
        assert history1["root_history"][1]["root_hash"] != bytes32([0] * 32)
        assert history1["root_history"][1]["confirmed"] is True
        assert history1["root_history"][1]["timestamp"] > 0
        key4 = b"d"
        value4 = b"\x06\x03"
        changelist = [{"action": "insert", "key": key4.hex(), "value": value4.hex()}]
        key5 = b"e"
        value5 = b"\x07\x01"
        changelist.append({"action": "insert", "key": key5.hex(), "value": value5.hex()})
        res = await data_rpc_api.batch_update({"id": store_id1.hex(), "changelist": changelist})
        update_tx_rec1 = res["tx_id"]
        await asyncio.sleep(1)
        for i in range(0, num_blocks):
            await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(ph))
            await asyncio.sleep(0.2)
        await time_out_assert(15, is_transaction_confirmed, True, "this is unused", wallet_rpc_api, update_tx_rec1)
        history2 = await data_rpc_api.get_root_history({"id": store_id1.hex()})
        assert len(history2["root_history"]) == 3
        assert history2["root_history"][0]["root_hash"] == bytes32([0] * 32)
        assert history2["root_history"][0]["confirmed"] is True
        assert history2["root_history"][0]["timestamp"] > 0
        assert history2["root_history"][1]["root_hash"] == history1["root_history"][1]["root_hash"]
        assert history2["root_history"][1]["confirmed"] is True
        assert history2["root_history"][1]["timestamp"] > history2["root_history"][0]["timestamp"]
        assert history2["root_history"][2]["confirmed"] is True
        assert history2["root_history"][2]["timestamp"] > history2["root_history"][1]["timestamp"]


@pytest.mark.asyncio
async def test_get_kv_diff(one_wallet_node_and_rpc: nodes_with_port, tmp_path: Path) -> None:
    wallet_node, full_node_api, wallet_rpc_port, bt = one_wallet_node_and_rpc
    num_blocks = 15
    assert wallet_node.server
    await wallet_node.server.start_client(PeerInfo("localhost", uint16(full_node_api.server._port)), None)
    ph = await wallet_node.wallet_state_manager.main_wallet.get_new_puzzlehash()
    for i in range(0, num_blocks):
        await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(ph))
        await asyncio.sleep(0.5)
    funds = sum(
        [calculate_pool_reward(uint32(i)) + calculate_base_farmer_reward(uint32(i)) for i in range(1, num_blocks)]
    )
    await time_out_assert(15, wallet_node.wallet_state_manager.main_wallet.get_confirmed_balance, funds)
    wallet_rpc_api = WalletRpcApi(wallet_node)
    async with init_data_layer(wallet_rpc_port=wallet_rpc_port, bt=bt, db_path=tmp_path) as data_layer:
        data_rpc_api = DataLayerRpcApi(data_layer)
        res = await data_rpc_api.create_data_store({})
        assert res is not None
        store_id1 = bytes32(hexstr_to_bytes(res["id"]))
        for i in range(0, num_blocks):
            await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(ph))
            await asyncio.sleep(0.2)

        res = await data_rpc_api.create_data_store({})
        assert res is not None

        key1 = b"a"
        value1 = b"\x01\x02"
        changelist: List[Dict[str, str]] = [{"action": "insert", "key": key1.hex(), "value": value1.hex()}]
        key2 = b"b"
        value2 = b"\x03\x02"
        changelist.append({"action": "insert", "key": key2.hex(), "value": value2.hex()})
        key3 = b"c"
        value3 = b"\x04\x05"
        changelist.append({"action": "insert", "key": key3.hex(), "value": value3.hex()})
        res = await data_rpc_api.batch_update({"id": store_id1.hex(), "changelist": changelist})
        update_tx_rec0 = res["tx_id"]
        await asyncio.sleep(1)
        for i in range(0, num_blocks):
            await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(ph))
            await asyncio.sleep(0.2)
        await time_out_assert(15, is_transaction_confirmed, True, "this is unused", wallet_rpc_api, update_tx_rec0)
        history = await data_rpc_api.get_root_history({"id": store_id1.hex()})
        diff_res = await data_rpc_api.get_kv_diff(
            {
                "id": store_id1.hex(),
                "hash_1": bytes32([0] * 32).hex(),
                "hash_2": history["root_history"][1]["root_hash"].hex(),
            }
        )
        assert len(diff_res["diff"]) == 3
        diff1 = {"type": "INSERT", "key": key1.hex(), "value": value1.hex()}
        diff2 = {"type": "INSERT", "key": key2.hex(), "value": value2.hex()}
        diff3 = {"type": "INSERT", "key": key3.hex(), "value": value3.hex()}
        assert diff1 in diff_res["diff"]
        assert diff2 in diff_res["diff"]
        assert diff3 in diff_res["diff"]
        key4 = b"d"
        value4 = b"\x06\x03"
        changelist = [{"action": "insert", "key": key4.hex(), "value": value4.hex()}]
        key5 = b"e"
        value5 = b"\x07\x01"
        changelist.append({"action": "insert", "key": key5.hex(), "value": value5.hex()})
        changelist.append({"action": "delete", "key": key1.hex()})
        res = await data_rpc_api.batch_update({"id": store_id1.hex(), "changelist": changelist})
        update_tx_rec1 = res["tx_id"]
        await asyncio.sleep(1)
        for i in range(0, num_blocks):
            await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(ph))
            await asyncio.sleep(0.2)
        await time_out_assert(15, is_transaction_confirmed, True, "this is unused", wallet_rpc_api, update_tx_rec1)
        history = await data_rpc_api.get_root_history({"id": store_id1.hex()})
        diff_res = await data_rpc_api.get_kv_diff(
            {
                "id": store_id1.hex(),
                "hash_1": history["root_history"][1]["root_hash"].hex(),
                "hash_2": history["root_history"][2]["root_hash"].hex(),
            }
        )
        assert len(diff_res["diff"]) == 3
        diff1 = {"type": "DELETE", "key": key1.hex(), "value": value1.hex()}
        diff4 = {"type": "INSERT", "key": key4.hex(), "value": value4.hex()}
        diff5 = {"type": "INSERT", "key": key5.hex(), "value": value5.hex()}
        assert diff4 in diff_res["diff"]
        assert diff5 in diff_res["diff"]
        assert diff1 in diff_res["diff"]


@pytest.mark.asyncio
async def test_batch_update_matches_single_operations(one_wallet_node_and_rpc: nodes_with_port, tmp_path: Path) -> None:
    wallet_node, full_node_api, wallet_rpc_port, bt = one_wallet_node_and_rpc
    num_blocks = 15
    assert wallet_node.server
    await wallet_node.server.start_client(PeerInfo("localhost", uint16(full_node_api.server._port)), None)
    ph = await wallet_node.wallet_state_manager.main_wallet.get_new_puzzlehash()
    for i in range(0, num_blocks):
        await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(ph))
        await asyncio.sleep(0.5)
    funds = sum(
        [calculate_pool_reward(uint32(i)) + calculate_base_farmer_reward(uint32(i)) for i in range(1, num_blocks)]
    )
    await time_out_assert(15, wallet_node.wallet_state_manager.main_wallet.get_confirmed_balance, funds)
    wallet_rpc_api = WalletRpcApi(wallet_node)
    async with init_data_layer(wallet_rpc_port=wallet_rpc_port, bt=bt, db_path=tmp_path) as data_layer:
        data_rpc_api = DataLayerRpcApi(data_layer)
        res = await data_rpc_api.create_data_store({})
        assert res is not None
        store_id = bytes32(hexstr_to_bytes(res["id"]))
        for i in range(0, num_blocks):
            await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(ph))
            await asyncio.sleep(0.2)

        key = b"a"
        value = b"\x00\x01"
        changelist: List[Dict[str, str]] = [{"action": "insert", "key": key.hex(), "value": value.hex()}]
        res = await data_rpc_api.batch_update({"id": store_id.hex(), "changelist": changelist})
        update_tx_rec0 = res["tx_id"]
        await asyncio.sleep(1)
        for i in range(0, num_blocks):
            await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(ph))
            await asyncio.sleep(0.2)
        await time_out_assert(15, is_transaction_confirmed, True, "this is unused", wallet_rpc_api, update_tx_rec0)

        key_2 = b"b"
        value_2 = b"\x00\x01"
        changelist = [{"action": "insert", "key": key_2.hex(), "value": value_2.hex()}]
        res = await data_rpc_api.batch_update({"id": store_id.hex(), "changelist": changelist})
        update_tx_rec1 = res["tx_id"]
        await asyncio.sleep(1)
        for i in range(0, num_blocks):
            await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(ph))
            await asyncio.sleep(0.2)
        await time_out_assert(15, is_transaction_confirmed, True, "this is unused", wallet_rpc_api, update_tx_rec1)

        key_3 = b"c"
        value_3 = b"\x00\x01"
        changelist = [{"action": "insert", "key": key_3.hex(), "value": value_3.hex()}]
        res = await data_rpc_api.batch_update({"id": store_id.hex(), "changelist": changelist})
        update_tx_rec2 = res["tx_id"]
        await asyncio.sleep(1)
        for i in range(0, num_blocks):
            await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(ph))
            await asyncio.sleep(0.2)
        await time_out_assert(15, is_transaction_confirmed, True, "this is unused", wallet_rpc_api, update_tx_rec2)

        changelist = [{"action": "delete", "key": key_3.hex()}]
        res = await data_rpc_api.batch_update({"id": store_id.hex(), "changelist": changelist})
        update_tx_rec3 = res["tx_id"]
        await asyncio.sleep(1)
        for i in range(0, num_blocks):
            await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(ph))
            await asyncio.sleep(0.2)
        await time_out_assert(15, is_transaction_confirmed, True, "this is unused", wallet_rpc_api, update_tx_rec3)

        root_1 = await data_rpc_api.get_roots({"ids": [store_id.hex()]})
        expected_res_hash = root_1["root_hashes"][0]["hash"]
        assert expected_res_hash != bytes32([0] * 32)

        changelist = [{"action": "delete", "key": key_2.hex()}]
        res = await data_rpc_api.batch_update({"id": store_id.hex(), "changelist": changelist})
        update_tx_rec4 = res["tx_id"]
        await asyncio.sleep(1)
        for i in range(0, num_blocks):
            await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(ph))
            await asyncio.sleep(0.2)
        await time_out_assert(15, is_transaction_confirmed, True, "this is unused", wallet_rpc_api, update_tx_rec4)

        changelist = [{"action": "delete", "key": key.hex()}]
        res = await data_rpc_api.batch_update({"id": store_id.hex(), "changelist": changelist})
        update_tx_rec5 = res["tx_id"]
        await asyncio.sleep(1)
        for i in range(0, num_blocks):
            await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(ph))
            await asyncio.sleep(0.2)
        await time_out_assert(15, is_transaction_confirmed, True, "this is unused", wallet_rpc_api, update_tx_rec5)

        root_2 = await data_rpc_api.get_roots({"ids": [store_id.hex()]})
        hash_2 = root_2["root_hashes"][0]["hash"]
        assert hash_2 == bytes32([0] * 32)

        changelist = [{"action": "insert", "key": key.hex(), "value": value.hex()}]
        changelist.append({"action": "insert", "key": key_2.hex(), "value": value_2.hex()})
        changelist.append({"action": "insert", "key": key_3.hex(), "value": value_3.hex()})
        changelist.append({"action": "delete", "key": key_3.hex()})

        res = await data_rpc_api.batch_update({"id": store_id.hex(), "changelist": changelist})
        update_tx_rec6 = res["tx_id"]
        await asyncio.sleep(1)
        for i in range(0, num_blocks):
            await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(ph))
            await asyncio.sleep(0.2)
        await time_out_assert(15, is_transaction_confirmed, True, "this is unused", wallet_rpc_api, update_tx_rec6)

        root_3 = await data_rpc_api.get_roots({"ids": [store_id.hex()]})
        batch_hash = root_3["root_hashes"][0]["hash"]
        assert batch_hash == expected_res_hash


@pytest.mark.asyncio
async def test_get_owned_stores(one_wallet_node_and_rpc: nodes_with_port, tmp_path: Path) -> None:
    wallet_node, full_node_api, wallet_rpc_port, bt = one_wallet_node_and_rpc
    num_blocks = 4
    assert wallet_node.server is not None
    await wallet_node.server.start_client(PeerInfo("localhost", uint16(full_node_api.server._port)), None)
    ph = await wallet_node.wallet_state_manager.main_wallet.get_new_puzzlehash()
    for i in range(0, num_blocks):
        await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(ph))
        await asyncio.sleep(0.5)
    funds = sum(
        [calculate_pool_reward(uint32(i)) + calculate_base_farmer_reward(uint32(i)) for i in range(1, num_blocks)]
    )
    await time_out_assert(15, wallet_node.wallet_state_manager.main_wallet.get_confirmed_balance, funds)

    async with init_data_layer(wallet_rpc_port=wallet_rpc_port, bt=bt, db_path=tmp_path) as data_layer:
        data_rpc_api = DataLayerRpcApi(data_layer)

        expected_store_ids = []

        for _ in range(3):
            res = await data_rpc_api.create_data_store({})
            assert res is not None
            launcher_id = bytes32.from_hexstr(res["id"])
            expected_store_ids.append(launcher_id)

        for i in range(0, num_blocks):
            await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(ph))
            await asyncio.sleep(0.5)

        response = await data_rpc_api.get_owned_stores(request={})
        store_ids = sorted(bytes32.from_hexstr(id) for id in response["store_ids"])

        assert store_ids == sorted(expected_store_ids)


@pytest.mark.asyncio
async def test_subscriptions(one_wallet_node_and_rpc: nodes_with_port, tmp_path: Path) -> None:
    wallet_node, full_node_api, wallet_rpc_port, bt = one_wallet_node_and_rpc
    num_blocks = 4
    assert wallet_node.server is not None
    await wallet_node.server.start_client(PeerInfo("localhost", uint16(full_node_api.server._port)), None)
    ph = await wallet_node.wallet_state_manager.main_wallet.get_new_puzzlehash()
    for i in range(0, num_blocks):
        await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(ph))
        await asyncio.sleep(0.5)
    funds = sum(
        [calculate_pool_reward(uint32(i)) + calculate_base_farmer_reward(uint32(i)) for i in range(1, num_blocks)]
    )
    await time_out_assert(15, wallet_node.wallet_state_manager.main_wallet.get_confirmed_balance, funds)

    async with init_data_layer(wallet_rpc_port=wallet_rpc_port, bt=bt, db_path=tmp_path) as data_layer:
        data_rpc_api = DataLayerRpcApi(data_layer)

        res = await data_rpc_api.create_data_store({})
        assert res is not None
        launcher_id = bytes32.from_hexstr(res["id"])

        for i in range(0, num_blocks):
            await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(ph))
            await asyncio.sleep(0.5)

        # This tests subscribe/unsubscribe to your own singletons, which isn't quite
        # the same thing as using a different wallet, but makes the tests much simpler
        response = await data_rpc_api.subscribe(request={"id": launcher_id.hex(), "urls": ["http://127.0.0.1/8000"]})
        assert response is not None

        # test subscriptions
        response = await data_rpc_api.subscriptions(request={})
        assert launcher_id.hex() in response.get("store_ids", [])

        # test unsubscribe
        response = await data_rpc_api.unsubscribe(request={"id": launcher_id.hex()})
        assert response is not None

        response = await data_rpc_api.subscriptions(request={})
        assert launcher_id.hex() not in response.get("store_ids", [])


@dataclass(frozen=True)
class StoreSetup:
    api: DataLayerRpcApi
    id: bytes32
    original_hash: bytes32
    data_layer: DataLayer


@dataclass(frozen=True)
class OfferSetup:
    maker: StoreSetup
    taker: StoreSetup
    full_node_api: FullNodeSimulator


@pytest_asyncio.fixture(name="offer_setup")
async def offer_setup_fixture(
    two_wallet_node_and_rpc: two_wallets_with_port,
    tmp_path: Path,
) -> AsyncIterator[OfferSetup]:
    wallet_nodes_and_ports, full_node_api, bt = two_wallet_node_and_rpc

    wallets: List[Wallet] = []
    for wallet_node, port in wallet_nodes_and_ports:
        assert wallet_node.server is not None
        await wallet_node.server.start_client(PeerInfo("localhost", uint16(full_node_api.server._port)), None)
        assert wallet_node.wallet_state_manager is not None
        wallet = wallet_node.wallet_state_manager.main_wallet
        wallets.append(wallet)

        await full_node_api.farm_blocks(count=1, wallet=wallet)

    async with contextlib.AsyncExitStack() as exit_stack:
        store_setups: List[StoreSetup] = []
        for wallet_node, port in wallet_nodes_and_ports:
            data_layer = await exit_stack.enter_async_context(
                init_data_layer(wallet_rpc_port=port, bt=bt, db_path=tmp_path.joinpath(str(port)))
            )
            data_rpc_api = DataLayerRpcApi(data_layer)

            create_response = await data_rpc_api.create_data_store({})
            await full_node_api.process_transaction_records(records=create_response["txs"])

            store_setups.append(
                StoreSetup(
                    api=data_rpc_api,
                    id=bytes32.from_hexstr(create_response["id"]),
                    original_hash=bytes32([0] * 32),
                    data_layer=data_layer,
                )
            )

        [maker, taker] = store_setups

        for sleep_time in backoff_times():
            await full_node_api.process_blocks(count=1)
            try:
                await maker.api.get_root({"id": maker.id.hex()})
                await taker.api.get_root({"id": taker.id.hex()})
            except Exception as e:
                # TODO: more specific exceptions...
                if "Failed to get root for" not in str(e):
                    raise
            else:
                break
            await asyncio.sleep(sleep_time)

        await maker.api.subscribe(request={"id": taker.id.hex(), "urls": ["http://127.0.0.1/8000"]})
        await taker.api.subscribe(request={"id": maker.id.hex(), "urls": ["http://127.0.0.1/8000"]})

        maker_original_singleton = await maker.data_layer.get_root(store_id=maker.id)
        assert maker_original_singleton is not None
        maker_original_root_hash = maker_original_singleton.root

        taker_original_singleton = await taker.data_layer.get_root(store_id=taker.id)
        assert taker_original_singleton is not None
        taker_original_root_hash = taker_original_singleton.root

        yield OfferSetup(
            maker=StoreSetup(
                api=maker.api,
                id=maker.id,
                original_hash=maker_original_root_hash,
                data_layer=maker.data_layer,
            ),
            taker=StoreSetup(
                api=taker.api,
                id=taker.id,
                original_hash=taker_original_root_hash,
                data_layer=taker.data_layer,
            ),
            full_node_api=full_node_api,
        )


async def populate_offer_setup(offer_setup: OfferSetup, count: int) -> OfferSetup:
    if count > 0:
        for store_setup, value_prefix in {offer_setup.maker: b"\x01", offer_setup.taker: b"\x02"}.items():
            await store_setup.api.batch_update(
                {
                    "id": store_setup.id.hex(),
                    "changelist": [
                        {
                            "action": "insert",
                            "key": value.to_bytes(length=1, byteorder="big").hex(),
                            "value": (value_prefix + value.to_bytes(length=1, byteorder="big")).hex(),
                        }
                        for value in range(count)
                    ],
                }
            )

        await process_for_data_layer_keys(
            expected_key=b"\x00",
            full_node_api=offer_setup.full_node_api,
            data_layer=offer_setup.maker.data_layer,
            store_id=offer_setup.maker.id,
        )
        await process_for_data_layer_keys(
            expected_key=b"\x00",
            full_node_api=offer_setup.full_node_api,
            data_layer=offer_setup.taker.data_layer,
            store_id=offer_setup.taker.id,
        )

    maker_original_singleton = await offer_setup.maker.data_layer.get_root(store_id=offer_setup.maker.id)
    assert maker_original_singleton is not None
    maker_original_root_hash = maker_original_singleton.root

    taker_original_singleton = await offer_setup.taker.data_layer.get_root(store_id=offer_setup.taker.id)
    assert taker_original_singleton is not None
    taker_original_root_hash = taker_original_singleton.root

    return OfferSetup(
        maker=StoreSetup(
            api=offer_setup.maker.api,
            id=offer_setup.maker.id,
            original_hash=maker_original_root_hash,
            data_layer=offer_setup.maker.data_layer,
        ),
        taker=StoreSetup(
            api=offer_setup.taker.api,
            id=offer_setup.taker.id,
            original_hash=taker_original_root_hash,
            data_layer=offer_setup.taker.data_layer,
        ),
        full_node_api=offer_setup.full_node_api,
    )


async def process_for_data_layer_keys(
    expected_key: bytes,
    full_node_api: FullNodeSimulator,
    data_layer: DataLayer,
    store_id: bytes32,
) -> None:
    for sleep_time in backoff_times():
        try:
            await data_layer.get_key_value_hash(store_id=store_id, key=expected_key)
        except Exception as e:
            # TODO: more specific exceptions...
            if "Key not found" not in str(e):
                raise
        else:
            break
        await full_node_api.process_blocks(count=1)
        await asyncio.sleep(sleep_time)
    else:
        raise Exception("failed to confirm the new data")


@dataclass(frozen=True)
class MakeAndTakeReference:
    entries_to_insert: int
    make_offer_response: Dict[str, Any]
    maker_inclusions: List[Dict[str, Any]]
    maker_root_history: List[bytes32]
    taker_inclusions: List[Dict[str, Any]]
    taker_root_history: List[bytes32]
    trade_id: str


make_one_take_one_reference = MakeAndTakeReference(
    entries_to_insert=10,
    make_offer_response={
        "trade_id": "743e8d83e14293156fb7adee9e132a70df726feef189ed24a69a81c11e56fb70",
        "offer": "0000000300000000000000000000000000000000000000000000000000000000000000002a5e6ce75ee92c329c576b5dfe550bd8a9054a7b3da5c5f74b1c2c55417fc05f0000000000000000ff02ffff01ff02ffff01ff02ffff03ffff18ff2fff3480ffff01ff04ffff04ff20ffff04ff2fff808080ffff04ffff02ff3effff04ff02ffff04ff05ffff04ffff02ff2affff04ff02ffff04ff27ffff04ffff02ffff03ff77ffff01ff02ff36ffff04ff02ffff04ff09ffff04ff57ffff04ffff02ff2effff04ff02ffff04ff05ff80808080ff808080808080ffff011d80ff0180ffff04ffff02ffff03ff77ffff0181b7ffff015780ff0180ff808080808080ffff04ff77ff808080808080ffff02ff3affff04ff02ffff04ff05ffff04ffff02ff0bff5f80ffff01ff8080808080808080ffff01ff088080ff0180ffff04ffff01ffffffff4947ff0233ffff0401ff0102ffffff20ff02ffff03ff05ffff01ff02ff32ffff04ff02ffff04ff0dffff04ffff0bff3cffff0bff34ff2480ffff0bff3cffff0bff3cffff0bff34ff2c80ff0980ffff0bff3cff0bffff0bff34ff8080808080ff8080808080ffff010b80ff0180ffff02ffff03ffff22ffff09ffff0dff0580ff2280ffff09ffff0dff0b80ff2280ffff15ff17ffff0181ff8080ffff01ff0bff05ff0bff1780ffff01ff088080ff0180ff02ffff03ff0bffff01ff02ffff03ffff02ff26ffff04ff02ffff04ff13ff80808080ffff01ff02ffff03ffff20ff1780ffff01ff02ffff03ffff09ff81b3ffff01818f80ffff01ff02ff3affff04ff02ffff04ff05ffff04ff1bffff04ff34ff808080808080ffff01ff04ffff04ff23ffff04ffff02ff36ffff04ff02ffff04ff09ffff04ff53ffff04ffff02ff2effff04ff02ffff04ff05ff80808080ff808080808080ff738080ffff02ff3affff04ff02ffff04ff05ffff04ff1bffff04ff34ff8080808080808080ff0180ffff01ff088080ff0180ffff01ff04ff13ffff02ff3affff04ff02ffff04ff05ffff04ff1bffff04ff17ff8080808080808080ff0180ffff01ff02ffff03ff17ff80ffff01ff088080ff018080ff0180ffffff02ffff03ffff09ff09ff3880ffff01ff02ffff03ffff18ff2dffff010180ffff01ff0101ff8080ff0180ff8080ff0180ff0bff3cffff0bff34ff2880ffff0bff3cffff0bff3cffff0bff34ff2c80ff0580ffff0bff3cffff02ff32ffff04ff02ffff04ff07ffff04ffff0bff34ff3480ff8080808080ffff0bff34ff8080808080ffff02ffff03ffff07ff0580ffff01ff0bffff0102ffff02ff2effff04ff02ffff04ff09ff80808080ffff02ff2effff04ff02ffff04ff0dff8080808080ffff01ff0bffff0101ff058080ff0180ff02ffff03ffff21ff17ffff09ff0bff158080ffff01ff04ff30ffff04ff0bff808080ffff01ff088080ff0180ff018080ffff04ffff01ffa07faa3253bfddd1e0decb0906b2dc6247bbc4cf608f58345d173adb63e8b47c9fffa07acfcbd1ed73bfe2b698508f4ea5ed353c60ace154360272ce91f9ab0c8423c3a0eff07522495060c066f66f32acc2a77e3a3e737aca8baea4d1a64ea4cdc13da9ffff04ffff01ff02ffff01ff02ffff01ff02ff3effff04ff02ffff04ff05ffff04ffff02ff2fff5f80ffff04ff80ffff04ffff04ffff04ff0bffff04ff17ff808080ffff01ff808080ffff01ff8080808080808080ffff04ffff01ffffff0233ff04ff0101ffff02ff02ffff03ff05ffff01ff02ff1affff04ff02ffff04ff0dffff04ffff0bff12ffff0bff2cff1480ffff0bff12ffff0bff12ffff0bff2cff3c80ff0980ffff0bff12ff0bffff0bff2cff8080808080ff8080808080ffff010b80ff0180ffff0bff12ffff0bff2cff1080ffff0bff12ffff0bff12ffff0bff2cff3c80ff0580ffff0bff12ffff02ff1affff04ff02ffff04ff07ffff04ffff0bff2cff2c80ff8080808080ffff0bff2cff8080808080ffff02ffff03ffff07ff0580ffff01ff0bffff0102ffff02ff2effff04ff02ffff04ff09ff80808080ffff02ff2effff04ff02ffff04ff0dff8080808080ffff01ff0bffff0101ff058080ff0180ff02ffff03ff0bffff01ff02ffff03ffff09ff23ff1880ffff01ff02ffff03ffff18ff81b3ff2c80ffff01ff02ffff03ffff20ff1780ffff01ff02ff3effff04ff02ffff04ff05ffff04ff1bffff04ff33ffff04ff2fffff04ff5fff8080808080808080ffff01ff088080ff0180ffff01ff04ff13ffff02ff3effff04ff02ffff04ff05ffff04ff1bffff04ff17ffff04ff2fffff04ff5fff80808080808080808080ff0180ffff01ff02ffff03ffff09ff23ffff0181e880ffff01ff02ff3effff04ff02ffff04ff05ffff04ff1bffff04ff17ffff04ffff02ffff03ffff22ffff09ffff02ff2effff04ff02ffff04ff53ff80808080ff82014f80ffff20ff5f8080ffff01ff02ff53ffff04ff818fffff04ff82014fffff04ff81b3ff8080808080ffff01ff088080ff0180ffff04ff2cff8080808080808080ffff01ff04ff13ffff02ff3effff04ff02ffff04ff05ffff04ff1bffff04ff17ffff04ff2fffff04ff5fff80808080808080808080ff018080ff0180ffff01ff04ffff04ff18ffff04ffff02ff16ffff04ff02ffff04ff05ffff04ff27ffff04ffff0bff2cff82014f80ffff04ffff02ff2effff04ff02ffff04ff818fff80808080ffff04ffff0bff2cff0580ff8080808080808080ff378080ff81af8080ff0180ff018080ffff04ffff01a0a04d9f57764f54a43e4030befb4d80026e870519aaa66334aef8304f5d0393c2ffff04ffff01ffa042f08ebc0578f2cec7a9ad1c3038e74e0f30eba5c2f4cb1ee1c8fdb682c19dbb80ffff04ffff01a057bfd1cb0adda3d94315053fda723f2028320faa8338225d99f629e3d46d43a9ffff04ffff01ff02ffff01ff02ff0affff04ff02ffff04ff03ff80808080ffff04ffff01ffff333effff02ffff03ff05ffff01ff04ffff04ff0cffff04ffff02ff1effff04ff02ffff04ff09ff80808080ff808080ffff02ff16ffff04ff02ffff04ff19ffff04ffff02ff0affff04ff02ffff04ff0dff80808080ff808080808080ff8080ff0180ffff02ffff03ff05ffff01ff04ffff04ff08ff0980ffff02ff16ffff04ff02ffff04ff0dffff04ff0bff808080808080ffff010b80ff0180ff02ffff03ffff07ff0580ffff01ff0bffff0102ffff02ff1effff04ff02ffff04ff09ff80808080ffff02ff1effff04ff02ffff04ff0dff8080808080ffff01ff0bffff0101ff058080ff0180ff018080ff018080808080ff01808080ffffa00000000000000000000000000000000000000000000000000000000000000000ffffa00000000000000000000000000000000000000000000000000000000000000000ff01ff8080808032dbe6d545f24635c7871ea53c623c358d7cea8f5e27a983ba6e5c0bf35fa243aa064e96a86637d8f5ebe153dc8645d29f43bee762d5ec10d06c8617fa60b8c50000000000000001ff02ffff01ff02ffff01ff02ffff03ffff18ff2fff3480ffff01ff04ffff04ff20ffff04ff2fff808080ffff04ffff02ff3effff04ff02ffff04ff05ffff04ffff02ff2affff04ff02ffff04ff27ffff04ffff02ffff03ff77ffff01ff02ff36ffff04ff02ffff04ff09ffff04ff57ffff04ffff02ff2effff04ff02ffff04ff05ff80808080ff808080808080ffff011d80ff0180ffff04ffff02ffff03ff77ffff0181b7ffff015780ff0180ff808080808080ffff04ff77ff808080808080ffff02ff3affff04ff02ffff04ff05ffff04ffff02ff0bff5f80ffff01ff8080808080808080ffff01ff088080ff0180ffff04ffff01ffffffff4947ff0233ffff0401ff0102ffffff20ff02ffff03ff05ffff01ff02ff32ffff04ff02ffff04ff0dffff04ffff0bff3cffff0bff34ff2480ffff0bff3cffff0bff3cffff0bff34ff2c80ff0980ffff0bff3cff0bffff0bff34ff8080808080ff8080808080ffff010b80ff0180ffff02ffff03ffff22ffff09ffff0dff0580ff2280ffff09ffff0dff0b80ff2280ffff15ff17ffff0181ff8080ffff01ff0bff05ff0bff1780ffff01ff088080ff0180ff02ffff03ff0bffff01ff02ffff03ffff02ff26ffff04ff02ffff04ff13ff80808080ffff01ff02ffff03ffff20ff1780ffff01ff02ffff03ffff09ff81b3ffff01818f80ffff01ff02ff3affff04ff02ffff04ff05ffff04ff1bffff04ff34ff808080808080ffff01ff04ffff04ff23ffff04ffff02ff36ffff04ff02ffff04ff09ffff04ff53ffff04ffff02ff2effff04ff02ffff04ff05ff80808080ff808080808080ff738080ffff02ff3affff04ff02ffff04ff05ffff04ff1bffff04ff34ff8080808080808080ff0180ffff01ff088080ff0180ffff01ff04ff13ffff02ff3affff04ff02ffff04ff05ffff04ff1bffff04ff17ff8080808080808080ff0180ffff01ff02ffff03ff17ff80ffff01ff088080ff018080ff0180ffffff02ffff03ffff09ff09ff3880ffff01ff02ffff03ffff18ff2dffff010180ffff01ff0101ff8080ff0180ff8080ff0180ff0bff3cffff0bff34ff2880ffff0bff3cffff0bff3cffff0bff34ff2c80ff0580ffff0bff3cffff02ff32ffff04ff02ffff04ff07ffff04ffff0bff34ff3480ff8080808080ffff0bff34ff8080808080ffff02ffff03ffff07ff0580ffff01ff0bffff0102ffff02ff2effff04ff02ffff04ff09ff80808080ffff02ff2effff04ff02ffff04ff0dff8080808080ffff01ff0bffff0101ff058080ff0180ff02ffff03ffff21ff17ffff09ff0bff158080ffff01ff04ff30ffff04ff0bff808080ffff01ff088080ff0180ff018080ffff04ffff01ffa07faa3253bfddd1e0decb0906b2dc6247bbc4cf608f58345d173adb63e8b47c9fffa0a14daf55d41ced6419bcd011fbc1f74ab9567fe55340d88435aa6493d628fa47a0eff07522495060c066f66f32acc2a77e3a3e737aca8baea4d1a64ea4cdc13da9ffff04ffff01ff02ffff01ff02ffff01ff02ff3effff04ff02ffff04ff05ffff04ffff02ff2fff5f80ffff04ff80ffff04ffff04ffff04ff0bffff04ff17ff808080ffff01ff808080ffff01ff8080808080808080ffff04ffff01ffffff0233ff04ff0101ffff02ff02ffff03ff05ffff01ff02ff1affff04ff02ffff04ff0dffff04ffff0bff12ffff0bff2cff1480ffff0bff12ffff0bff12ffff0bff2cff3c80ff0980ffff0bff12ff0bffff0bff2cff8080808080ff8080808080ffff010b80ff0180ffff0bff12ffff0bff2cff1080ffff0bff12ffff0bff12ffff0bff2cff3c80ff0580ffff0bff12ffff02ff1affff04ff02ffff04ff07ffff04ffff0bff2cff2c80ff8080808080ffff0bff2cff8080808080ffff02ffff03ffff07ff0580ffff01ff0bffff0102ffff02ff2effff04ff02ffff04ff09ff80808080ffff02ff2effff04ff02ffff04ff0dff8080808080ffff01ff0bffff0101ff058080ff0180ff02ffff03ff0bffff01ff02ffff03ffff09ff23ff1880ffff01ff02ffff03ffff18ff81b3ff2c80ffff01ff02ffff03ffff20ff1780ffff01ff02ff3effff04ff02ffff04ff05ffff04ff1bffff04ff33ffff04ff2fffff04ff5fff8080808080808080ffff01ff088080ff0180ffff01ff04ff13ffff02ff3effff04ff02ffff04ff05ffff04ff1bffff04ff17ffff04ff2fffff04ff5fff80808080808080808080ff0180ffff01ff02ffff03ffff09ff23ffff0181e880ffff01ff02ff3effff04ff02ffff04ff05ffff04ff1bffff04ff17ffff04ffff02ffff03ffff22ffff09ffff02ff2effff04ff02ffff04ff53ff80808080ff82014f80ffff20ff5f8080ffff01ff02ff53ffff04ff818fffff04ff82014fffff04ff81b3ff8080808080ffff01ff088080ff0180ffff04ff2cff8080808080808080ffff01ff04ff13ffff02ff3effff04ff02ffff04ff05ffff04ff1bffff04ff17ffff04ff2fffff04ff5fff80808080808080808080ff018080ff0180ffff01ff04ffff04ff18ffff04ffff02ff16ffff04ff02ffff04ff05ffff04ff27ffff04ffff0bff2cff82014f80ffff04ffff02ff2effff04ff02ffff04ff818fff80808080ffff04ffff0bff2cff0580ff8080808080808080ff378080ff81af8080ff0180ff018080ffff04ffff01a0a04d9f57764f54a43e4030befb4d80026e870519aaa66334aef8304f5d0393c2ffff04ffff01ffa06661ea6604b491118b0f49c932c0f0de2ad815a57b54b6ec8fdbd1b408ae7e2780ffff04ffff01a057bfd1cb0adda3d94315053fda723f2028320faa8338225d99f629e3d46d43a9ffff04ffff01ff02ffff01ff02ffff01ff02ffff03ff0bffff01ff02ffff03ffff09ff05ffff1dff0bffff1effff0bff0bffff02ff06ffff04ff02ffff04ff17ff8080808080808080ffff01ff02ff17ff2f80ffff01ff088080ff0180ffff01ff04ffff04ff04ffff04ff05ffff04ffff02ff06ffff04ff02ffff04ff17ff80808080ff80808080ffff02ff17ff2f808080ff0180ffff04ffff01ff32ff02ffff03ffff07ff0580ffff01ff0bffff0102ffff02ff06ffff04ff02ffff04ff09ff80808080ffff02ff06ffff04ff02ffff04ff0dff8080808080ffff01ff0bffff0101ff058080ff0180ff018080ffff04ffff01b0a132fae32c98cbb7d8f5814c49ee3f0ba6ec2172c5e5f6900655a65cd2157a06a1c6eb89c68c8d2cdcee9506c2217978ff018080ff018080808080ff01808080ffffa0a14daf55d41ced6419bcd011fbc1f74ab9567fe55340d88435aa6493d628fa47ffa01804338c97f989c78d88716206c0f27315f3eb7d59417ab2eacee20f0a7ff60bff0180ff01ffffff80ffff02ffff01ff02ffff01ff02ffff03ff5fffff01ff02ff3affff04ff02ffff04ff0bffff04ff17ffff04ff2fffff04ff5fffff04ff81bfffff04ff82017fffff04ff8202ffffff04ffff02ff05ff8205ff80ff8080808080808080808080ffff01ff04ffff04ff10ffff01ff81ff8080ffff02ff05ff8205ff808080ff0180ffff04ffff01ffffff49ff3f02ff04ff0101ffff02ffff02ffff03ff05ffff01ff02ff2affff04ff02ffff04ff0dffff04ffff0bff12ffff0bff2cff1480ffff0bff12ffff0bff12ffff0bff2cff3c80ff0980ffff0bff12ff0bffff0bff2cff8080808080ff8080808080ffff010b80ff0180ff02ffff03ff05ffff01ff02ffff03ffff02ff3effff04ff02ffff04ff82011fffff04ff27ffff04ff4fff808080808080ffff01ff02ff3affff04ff02ffff04ff0dffff04ff1bffff04ff37ffff04ff6fffff04ff81dfffff04ff8201bfffff04ff82037fffff04ffff04ffff04ff28ffff04ffff0bffff02ff26ffff04ff02ffff04ff11ffff04ffff02ff26ffff04ff02ffff04ff13ffff04ff82027fffff04ffff02ff36ffff04ff02ffff04ff82013fff80808080ffff04ffff02ff36ffff04ff02ffff04ff819fff80808080ffff04ffff02ff36ffff04ff02ffff04ff13ff80808080ff8080808080808080ffff04ffff02ff36ffff04ff02ffff04ff09ff80808080ff808080808080ffff012480ff808080ff8202ff80ff8080808080808080808080ffff01ff088080ff0180ffff018202ff80ff0180ffffff0bff12ffff0bff2cff3880ffff0bff12ffff0bff12ffff0bff2cff3c80ff0580ffff0bff12ffff02ff2affff04ff02ffff04ff07ffff04ffff0bff2cff2c80ff8080808080ffff0bff2cff8080808080ff02ffff03ffff07ff0580ffff01ff0bffff0102ffff02ff36ffff04ff02ffff04ff09ff80808080ffff02ff36ffff04ff02ffff04ff0dff8080808080ffff01ff0bffff0101ff058080ff0180ffff02ffff03ff1bffff01ff02ff2effff04ff02ffff04ffff02ffff03ffff18ffff0101ff1380ffff01ff0bffff0102ff2bff0580ffff01ff0bffff0102ff05ff2b8080ff0180ffff04ffff04ffff17ff13ffff0181ff80ff3b80ff8080808080ffff010580ff0180ff02ffff03ff17ffff01ff02ffff03ffff09ff05ffff02ff2effff04ff02ffff04ff13ffff04ff27ff808080808080ffff01ff02ff3effff04ff02ffff04ff05ffff04ff1bffff04ff37ff808080808080ffff01ff088080ff0180ffff01ff010180ff0180ff018080ffff04ffff01ff01ffff81e8ff0bffffffffa08e54f5066aa7999fc1561a56df59d11ff01f7df93cadf49a61adebf65dec65ea80ffa057bfd1cb0adda3d94315053fda723f2028320faa8338225d99f629e3d46d43a980ff808080ffff33ffa0ca77e42ac3b3375edc54af271f21d075afd02d72969cababeec63e22f7ab10deff01ffffa0a14daf55d41ced6419bcd011fbc1f74ab9567fe55340d88435aa6493d628fa47ffa08e54f5066aa7999fc1561a56df59d11ff01f7df93cadf49a61adebf65dec65eaffa0c842b1a384b8633ac25d0f12bd7b614f86a77642ab6426418750f2b0b86bab2a8080ffff3fffa0bd7aa54c5f93ef1738439aa60b471ce2aa4c62fb18a7943aa10061f00dbdb8368080ffff04ffff01ffffa07faa3253bfddd1e0decb0906b2dc6247bbc4cf608f58345d173adb63e8b47c9fffa07acfcbd1ed73bfe2b698508f4ea5ed353c60ace154360272ce91f9ab0c8423c3a0eff07522495060c066f66f32acc2a77e3a3e737aca8baea4d1a64ea4cdc13da980ffff04ffff01ffa0a04d9f57764f54a43e4030befb4d80026e870519aaa66334aef8304f5d0393c280ffff04ffff01ffffa07f3e180acdf046f955d3440bb3a16dfd6f5a46c809cee98e7514127327b1cab58080ff018080808080ffff80ff80ff80ff80ff8080808080ca2e21c90d263e63b73d449a3f8d57b9458846f7af27d9a61a515395fa14071ea55bba78c76265b4bac257251b1e89dd13637a7c18e8dcb03e092dfb7eb5a84a0000000000000001ff02ffff01ff02ffff01ff02ffff03ffff18ff2fff3480ffff01ff04ffff04ff20ffff04ff2fff808080ffff04ffff02ff3effff04ff02ffff04ff05ffff04ffff02ff2affff04ff02ffff04ff27ffff04ffff02ffff03ff77ffff01ff02ff36ffff04ff02ffff04ff09ffff04ff57ffff04ffff02ff2effff04ff02ffff04ff05ff80808080ff808080808080ffff011d80ff0180ffff04ffff02ffff03ff77ffff0181b7ffff015780ff0180ff808080808080ffff04ff77ff808080808080ffff02ff3affff04ff02ffff04ff05ffff04ffff02ff0bff5f80ffff01ff8080808080808080ffff01ff088080ff0180ffff04ffff01ffffffff4947ff0233ffff0401ff0102ffffff20ff02ffff03ff05ffff01ff02ff32ffff04ff02ffff04ff0dffff04ffff0bff3cffff0bff34ff2480ffff0bff3cffff0bff3cffff0bff34ff2c80ff0980ffff0bff3cff0bffff0bff34ff8080808080ff8080808080ffff010b80ff0180ffff02ffff03ffff22ffff09ffff0dff0580ff2280ffff09ffff0dff0b80ff2280ffff15ff17ffff0181ff8080ffff01ff0bff05ff0bff1780ffff01ff088080ff0180ff02ffff03ff0bffff01ff02ffff03ffff02ff26ffff04ff02ffff04ff13ff80808080ffff01ff02ffff03ffff20ff1780ffff01ff02ffff03ffff09ff81b3ffff01818f80ffff01ff02ff3affff04ff02ffff04ff05ffff04ff1bffff04ff34ff808080808080ffff01ff04ffff04ff23ffff04ffff02ff36ffff04ff02ffff04ff09ffff04ff53ffff04ffff02ff2effff04ff02ffff04ff05ff80808080ff808080808080ff738080ffff02ff3affff04ff02ffff04ff05ffff04ff1bffff04ff34ff8080808080808080ff0180ffff01ff088080ff0180ffff01ff04ff13ffff02ff3affff04ff02ffff04ff05ffff04ff1bffff04ff17ff8080808080808080ff0180ffff01ff02ffff03ff17ff80ffff01ff088080ff018080ff0180ffffff02ffff03ffff09ff09ff3880ffff01ff02ffff03ffff18ff2dffff010180ffff01ff0101ff8080ff0180ff8080ff0180ff0bff3cffff0bff34ff2880ffff0bff3cffff0bff3cffff0bff34ff2c80ff0580ffff0bff3cffff02ff32ffff04ff02ffff04ff07ffff04ffff0bff34ff3480ff8080808080ffff0bff34ff8080808080ffff02ffff03ffff07ff0580ffff01ff0bffff0102ffff02ff2effff04ff02ffff04ff09ff80808080ffff02ff2effff04ff02ffff04ff0dff8080808080ffff01ff0bffff0101ff058080ff0180ff02ffff03ffff21ff17ffff09ff0bff158080ffff01ff04ff30ffff04ff0bff808080ffff01ff088080ff0180ff018080ffff04ffff01ffa07faa3253bfddd1e0decb0906b2dc6247bbc4cf608f58345d173adb63e8b47c9fffa0a14daf55d41ced6419bcd011fbc1f74ab9567fe55340d88435aa6493d628fa47a0eff07522495060c066f66f32acc2a77e3a3e737aca8baea4d1a64ea4cdc13da9ffff04ffff01ff02ffff01ff02ffff01ff02ff3effff04ff02ffff04ff05ffff04ffff02ff2fff5f80ffff04ff80ffff04ffff04ffff04ff0bffff04ff17ff808080ffff01ff808080ffff01ff8080808080808080ffff04ffff01ffffff0233ff04ff0101ffff02ff02ffff03ff05ffff01ff02ff1affff04ff02ffff04ff0dffff04ffff0bff12ffff0bff2cff1480ffff0bff12ffff0bff12ffff0bff2cff3c80ff0980ffff0bff12ff0bffff0bff2cff8080808080ff8080808080ffff010b80ff0180ffff0bff12ffff0bff2cff1080ffff0bff12ffff0bff12ffff0bff2cff3c80ff0580ffff0bff12ffff02ff1affff04ff02ffff04ff07ffff04ffff0bff2cff2c80ff8080808080ffff0bff2cff8080808080ffff02ffff03ffff07ff0580ffff01ff0bffff0102ffff02ff2effff04ff02ffff04ff09ff80808080ffff02ff2effff04ff02ffff04ff0dff8080808080ffff01ff0bffff0101ff058080ff0180ff02ffff03ff0bffff01ff02ffff03ffff09ff23ff1880ffff01ff02ffff03ffff18ff81b3ff2c80ffff01ff02ffff03ffff20ff1780ffff01ff02ff3effff04ff02ffff04ff05ffff04ff1bffff04ff33ffff04ff2fffff04ff5fff8080808080808080ffff01ff088080ff0180ffff01ff04ff13ffff02ff3effff04ff02ffff04ff05ffff04ff1bffff04ff17ffff04ff2fffff04ff5fff80808080808080808080ff0180ffff01ff02ffff03ffff09ff23ffff0181e880ffff01ff02ff3effff04ff02ffff04ff05ffff04ff1bffff04ff17ffff04ffff02ffff03ffff22ffff09ffff02ff2effff04ff02ffff04ff53ff80808080ff82014f80ffff20ff5f8080ffff01ff02ff53ffff04ff818fffff04ff82014fffff04ff81b3ff8080808080ffff01ff088080ff0180ffff04ff2cff8080808080808080ffff01ff04ff13ffff02ff3effff04ff02ffff04ff05ffff04ff1bffff04ff17ffff04ff2fffff04ff5fff80808080808080808080ff018080ff0180ffff01ff04ffff04ff18ffff04ffff02ff16ffff04ff02ffff04ff05ffff04ff27ffff04ffff0bff2cff82014f80ffff04ffff02ff2effff04ff02ffff04ff818fff80808080ffff04ffff0bff2cff0580ff8080808080808080ff378080ff81af8080ff0180ff018080ffff04ffff01a0a04d9f57764f54a43e4030befb4d80026e870519aaa66334aef8304f5d0393c2ffff04ffff01ffa08e54f5066aa7999fc1561a56df59d11ff01f7df93cadf49a61adebf65dec65ea80ffff04ffff01a057bfd1cb0adda3d94315053fda723f2028320faa8338225d99f629e3d46d43a9ffff04ffff01ff01ffff33ffa0c842b1a384b8633ac25d0f12bd7b614f86a77642ab6426418750f2b0b86bab2aff01ffffa0a14daf55d41ced6419bcd011fbc1f74ab9567fe55340d88435aa6493d628fa47ffa08e54f5066aa7999fc1561a56df59d11ff01f7df93cadf49a61adebf65dec65eaffa0c842b1a384b8633ac25d0f12bd7b614f86a77642ab6426418750f2b0b86bab2a8080ffff3eff248080ff018080808080ff01808080ffffa032dbe6d545f24635c7871ea53c623c358d7cea8f5e27a983ba6e5c0bf35fa243ffa08c4aebb18e8ce08405083c3d90a29f30239865142e2dcbca5393f40df9e3821dff0180ff01ffff8080809286d9f1538e659e58131d4883714e551cff2a80d55613a4375ea3d8fe2bca9e1714c9f7ac914a2ff4b7df52cb8183400c8e03bd765510dd1787a4ce66e5ca6ad03ec39cdb90c84e8e8b58cc07d318e4b397c8c163aecb038796eb10993ee824",  # noqa: E501
        "taker": [
            {
                "store_id": "7acfcbd1ed73bfe2b698508f4ea5ed353c60ace154360272ce91f9ab0c8423c3",
                "inclusions": [{"key": "10", "value": "0210"}],
            }
        ],
        "maker": [
            {
                "store_id": "a14daf55d41ced6419bcd011fbc1f74ab9567fe55340d88435aa6493d628fa47",
                "proofs": [
                    {
                        "key": "10",
                        "value": "0110",
                        "node_hash": "de4ec93c032f5117d8af076dfc86faa5987a6c0b1d52ffc9cf0dfa43989d8c58",
                        "layers": [
                            {
                                "other_hash_side": "left",
                                "other_hash": "1c8ab812b97f5a9da0ba4be2380104810fe5c8022efe9b9e2c9d188fc3537434",
                                "combined_hash": "d340000b3a6717a5a8d42b24516ff69430235c771f8a527554b357b7f03c6de0",
                            },
                            {
                                "other_hash_side": "right",
                                "other_hash": "54e8b4cac761778f396840b343c0f1cb0e1fd0c9927d48d2f0d09a7a6f225126",
                                "combined_hash": "7676004a15439e4e8345d0f9f3a15500805b3447285904b7bcd7d14e27381d2e",
                            },
                            {
                                "other_hash_side": "right",
                                "other_hash": "6a37ca2d9a37a50f2d53387c3cf31395c72d75b1aacfa4402c32dc6d354542b4",
                                "combined_hash": "b1dc97f797a32631483c11d33b4759f5b498b512b7436286d1dc00bb1024b7e2",
                            },
                            {
                                "other_hash_side": "right",
                                "other_hash": "bcff6f16886339a196a2f6c842ad6d350a8579d123eb8602a0a85965ba25d671",
                                "combined_hash": "8e54f5066aa7999fc1561a56df59d11ff01f7df93cadf49a61adebf65dec65ea",
                            },
                        ],
                    }
                ],
            }
        ],
    },
    maker_inclusions=[{"key": b"\x10".hex(), "value": b"\x01\x10".hex()}],
    taker_inclusions=[{"key": b"\x10".hex(), "value": b"\x02\x10".hex()}],
    trade_id="e7c04330c93388b5cdcb7f5e0a7535fa69cac2b59de494a487e915b2c822cf31",
    maker_root_history=[
        bytes32.from_hexstr("6661ea6604b491118b0f49c932c0f0de2ad815a57b54b6ec8fdbd1b408ae7e27"),
        bytes32.from_hexstr("8e54f5066aa7999fc1561a56df59d11ff01f7df93cadf49a61adebf65dec65ea"),
    ],
    taker_root_history=[
        bytes32.from_hexstr("42f08ebc0578f2cec7a9ad1c3038e74e0f30eba5c2f4cb1ee1c8fdb682c19dbb"),
        bytes32.from_hexstr("eeb63ac765065d2ee161e1c059c8188ef809e1c3ed8739bad5bfee2c2ee1c742"),
    ],
)


make_two_take_one_reference = MakeAndTakeReference(
    entries_to_insert=10,
    make_offer_response={
        "trade_id": "c52958d8d018254c036250dfd3619ee9b7e2f68012e4dbc5dc86eb281b07beaf",
        "offer": "0000000300000000000000000000000000000000000000000000000000000000000000002a5e6ce75ee92c329c576b5dfe550bd8a9054a7b3da5c5f74b1c2c55417fc05f0000000000000000ff02ffff01ff02ffff01ff02ffff03ffff18ff2fff3480ffff01ff04ffff04ff20ffff04ff2fff808080ffff04ffff02ff3effff04ff02ffff04ff05ffff04ffff02ff2affff04ff02ffff04ff27ffff04ffff02ffff03ff77ffff01ff02ff36ffff04ff02ffff04ff09ffff04ff57ffff04ffff02ff2effff04ff02ffff04ff05ff80808080ff808080808080ffff011d80ff0180ffff04ffff02ffff03ff77ffff0181b7ffff015780ff0180ff808080808080ffff04ff77ff808080808080ffff02ff3affff04ff02ffff04ff05ffff04ffff02ff0bff5f80ffff01ff8080808080808080ffff01ff088080ff0180ffff04ffff01ffffffff4947ff0233ffff0401ff0102ffffff20ff02ffff03ff05ffff01ff02ff32ffff04ff02ffff04ff0dffff04ffff0bff3cffff0bff34ff2480ffff0bff3cffff0bff3cffff0bff34ff2c80ff0980ffff0bff3cff0bffff0bff34ff8080808080ff8080808080ffff010b80ff0180ffff02ffff03ffff22ffff09ffff0dff0580ff2280ffff09ffff0dff0b80ff2280ffff15ff17ffff0181ff8080ffff01ff0bff05ff0bff1780ffff01ff088080ff0180ff02ffff03ff0bffff01ff02ffff03ffff02ff26ffff04ff02ffff04ff13ff80808080ffff01ff02ffff03ffff20ff1780ffff01ff02ffff03ffff09ff81b3ffff01818f80ffff01ff02ff3affff04ff02ffff04ff05ffff04ff1bffff04ff34ff808080808080ffff01ff04ffff04ff23ffff04ffff02ff36ffff04ff02ffff04ff09ffff04ff53ffff04ffff02ff2effff04ff02ffff04ff05ff80808080ff808080808080ff738080ffff02ff3affff04ff02ffff04ff05ffff04ff1bffff04ff34ff8080808080808080ff0180ffff01ff088080ff0180ffff01ff04ff13ffff02ff3affff04ff02ffff04ff05ffff04ff1bffff04ff17ff8080808080808080ff0180ffff01ff02ffff03ff17ff80ffff01ff088080ff018080ff0180ffffff02ffff03ffff09ff09ff3880ffff01ff02ffff03ffff18ff2dffff010180ffff01ff0101ff8080ff0180ff8080ff0180ff0bff3cffff0bff34ff2880ffff0bff3cffff0bff3cffff0bff34ff2c80ff0580ffff0bff3cffff02ff32ffff04ff02ffff04ff07ffff04ffff0bff34ff3480ff8080808080ffff0bff34ff8080808080ffff02ffff03ffff07ff0580ffff01ff0bffff0102ffff02ff2effff04ff02ffff04ff09ff80808080ffff02ff2effff04ff02ffff04ff0dff8080808080ffff01ff0bffff0101ff058080ff0180ff02ffff03ffff21ff17ffff09ff0bff158080ffff01ff04ff30ffff04ff0bff808080ffff01ff088080ff0180ff018080ffff04ffff01ffa07faa3253bfddd1e0decb0906b2dc6247bbc4cf608f58345d173adb63e8b47c9fffa07acfcbd1ed73bfe2b698508f4ea5ed353c60ace154360272ce91f9ab0c8423c3a0eff07522495060c066f66f32acc2a77e3a3e737aca8baea4d1a64ea4cdc13da9ffff04ffff01ff02ffff01ff02ffff01ff02ff3effff04ff02ffff04ff05ffff04ffff02ff2fff5f80ffff04ff80ffff04ffff04ffff04ff0bffff04ff17ff808080ffff01ff808080ffff01ff8080808080808080ffff04ffff01ffffff0233ff04ff0101ffff02ff02ffff03ff05ffff01ff02ff1affff04ff02ffff04ff0dffff04ffff0bff12ffff0bff2cff1480ffff0bff12ffff0bff12ffff0bff2cff3c80ff0980ffff0bff12ff0bffff0bff2cff8080808080ff8080808080ffff010b80ff0180ffff0bff12ffff0bff2cff1080ffff0bff12ffff0bff12ffff0bff2cff3c80ff0580ffff0bff12ffff02ff1affff04ff02ffff04ff07ffff04ffff0bff2cff2c80ff8080808080ffff0bff2cff8080808080ffff02ffff03ffff07ff0580ffff01ff0bffff0102ffff02ff2effff04ff02ffff04ff09ff80808080ffff02ff2effff04ff02ffff04ff0dff8080808080ffff01ff0bffff0101ff058080ff0180ff02ffff03ff0bffff01ff02ffff03ffff09ff23ff1880ffff01ff02ffff03ffff18ff81b3ff2c80ffff01ff02ffff03ffff20ff1780ffff01ff02ff3effff04ff02ffff04ff05ffff04ff1bffff04ff33ffff04ff2fffff04ff5fff8080808080808080ffff01ff088080ff0180ffff01ff04ff13ffff02ff3effff04ff02ffff04ff05ffff04ff1bffff04ff17ffff04ff2fffff04ff5fff80808080808080808080ff0180ffff01ff02ffff03ffff09ff23ffff0181e880ffff01ff02ff3effff04ff02ffff04ff05ffff04ff1bffff04ff17ffff04ffff02ffff03ffff22ffff09ffff02ff2effff04ff02ffff04ff53ff80808080ff82014f80ffff20ff5f8080ffff01ff02ff53ffff04ff818fffff04ff82014fffff04ff81b3ff8080808080ffff01ff088080ff0180ffff04ff2cff8080808080808080ffff01ff04ff13ffff02ff3effff04ff02ffff04ff05ffff04ff1bffff04ff17ffff04ff2fffff04ff5fff80808080808080808080ff018080ff0180ffff01ff04ffff04ff18ffff04ffff02ff16ffff04ff02ffff04ff05ffff04ff27ffff04ffff0bff2cff82014f80ffff04ffff02ff2effff04ff02ffff04ff818fff80808080ffff04ffff0bff2cff0580ff8080808080808080ff378080ff81af8080ff0180ff018080ffff04ffff01a0a04d9f57764f54a43e4030befb4d80026e870519aaa66334aef8304f5d0393c2ffff04ffff01ffa042f08ebc0578f2cec7a9ad1c3038e74e0f30eba5c2f4cb1ee1c8fdb682c19dbb80ffff04ffff01a057bfd1cb0adda3d94315053fda723f2028320faa8338225d99f629e3d46d43a9ffff04ffff01ff02ffff01ff02ff0affff04ff02ffff04ff03ff80808080ffff04ffff01ffff333effff02ffff03ff05ffff01ff04ffff04ff0cffff04ffff02ff1effff04ff02ffff04ff09ff80808080ff808080ffff02ff16ffff04ff02ffff04ff19ffff04ffff02ff0affff04ff02ffff04ff0dff80808080ff808080808080ff8080ff0180ffff02ffff03ff05ffff01ff04ffff04ff08ff0980ffff02ff16ffff04ff02ffff04ff0dffff04ff0bff808080808080ffff010b80ff0180ff02ffff03ffff07ff0580ffff01ff0bffff0102ffff02ff1effff04ff02ffff04ff09ff80808080ffff02ff1effff04ff02ffff04ff0dff8080808080ffff01ff0bffff0101ff058080ff0180ff018080ff018080808080ff01808080ffffa00000000000000000000000000000000000000000000000000000000000000000ffffa00000000000000000000000000000000000000000000000000000000000000000ff01ff8080808032dbe6d545f24635c7871ea53c623c358d7cea8f5e27a983ba6e5c0bf35fa243aa064e96a86637d8f5ebe153dc8645d29f43bee762d5ec10d06c8617fa60b8c50000000000000001ff02ffff01ff02ffff01ff02ffff03ffff18ff2fff3480ffff01ff04ffff04ff20ffff04ff2fff808080ffff04ffff02ff3effff04ff02ffff04ff05ffff04ffff02ff2affff04ff02ffff04ff27ffff04ffff02ffff03ff77ffff01ff02ff36ffff04ff02ffff04ff09ffff04ff57ffff04ffff02ff2effff04ff02ffff04ff05ff80808080ff808080808080ffff011d80ff0180ffff04ffff02ffff03ff77ffff0181b7ffff015780ff0180ff808080808080ffff04ff77ff808080808080ffff02ff3affff04ff02ffff04ff05ffff04ffff02ff0bff5f80ffff01ff8080808080808080ffff01ff088080ff0180ffff04ffff01ffffffff4947ff0233ffff0401ff0102ffffff20ff02ffff03ff05ffff01ff02ff32ffff04ff02ffff04ff0dffff04ffff0bff3cffff0bff34ff2480ffff0bff3cffff0bff3cffff0bff34ff2c80ff0980ffff0bff3cff0bffff0bff34ff8080808080ff8080808080ffff010b80ff0180ffff02ffff03ffff22ffff09ffff0dff0580ff2280ffff09ffff0dff0b80ff2280ffff15ff17ffff0181ff8080ffff01ff0bff05ff0bff1780ffff01ff088080ff0180ff02ffff03ff0bffff01ff02ffff03ffff02ff26ffff04ff02ffff04ff13ff80808080ffff01ff02ffff03ffff20ff1780ffff01ff02ffff03ffff09ff81b3ffff01818f80ffff01ff02ff3affff04ff02ffff04ff05ffff04ff1bffff04ff34ff808080808080ffff01ff04ffff04ff23ffff04ffff02ff36ffff04ff02ffff04ff09ffff04ff53ffff04ffff02ff2effff04ff02ffff04ff05ff80808080ff808080808080ff738080ffff02ff3affff04ff02ffff04ff05ffff04ff1bffff04ff34ff8080808080808080ff0180ffff01ff088080ff0180ffff01ff04ff13ffff02ff3affff04ff02ffff04ff05ffff04ff1bffff04ff17ff8080808080808080ff0180ffff01ff02ffff03ff17ff80ffff01ff088080ff018080ff0180ffffff02ffff03ffff09ff09ff3880ffff01ff02ffff03ffff18ff2dffff010180ffff01ff0101ff8080ff0180ff8080ff0180ff0bff3cffff0bff34ff2880ffff0bff3cffff0bff3cffff0bff34ff2c80ff0580ffff0bff3cffff02ff32ffff04ff02ffff04ff07ffff04ffff0bff34ff3480ff8080808080ffff0bff34ff8080808080ffff02ffff03ffff07ff0580ffff01ff0bffff0102ffff02ff2effff04ff02ffff04ff09ff80808080ffff02ff2effff04ff02ffff04ff0dff8080808080ffff01ff0bffff0101ff058080ff0180ff02ffff03ffff21ff17ffff09ff0bff158080ffff01ff04ff30ffff04ff0bff808080ffff01ff088080ff0180ff018080ffff04ffff01ffa07faa3253bfddd1e0decb0906b2dc6247bbc4cf608f58345d173adb63e8b47c9fffa0a14daf55d41ced6419bcd011fbc1f74ab9567fe55340d88435aa6493d628fa47a0eff07522495060c066f66f32acc2a77e3a3e737aca8baea4d1a64ea4cdc13da9ffff04ffff01ff02ffff01ff02ffff01ff02ff3effff04ff02ffff04ff05ffff04ffff02ff2fff5f80ffff04ff80ffff04ffff04ffff04ff0bffff04ff17ff808080ffff01ff808080ffff01ff8080808080808080ffff04ffff01ffffff0233ff04ff0101ffff02ff02ffff03ff05ffff01ff02ff1affff04ff02ffff04ff0dffff04ffff0bff12ffff0bff2cff1480ffff0bff12ffff0bff12ffff0bff2cff3c80ff0980ffff0bff12ff0bffff0bff2cff8080808080ff8080808080ffff010b80ff0180ffff0bff12ffff0bff2cff1080ffff0bff12ffff0bff12ffff0bff2cff3c80ff0580ffff0bff12ffff02ff1affff04ff02ffff04ff07ffff04ffff0bff2cff2c80ff8080808080ffff0bff2cff8080808080ffff02ffff03ffff07ff0580ffff01ff0bffff0102ffff02ff2effff04ff02ffff04ff09ff80808080ffff02ff2effff04ff02ffff04ff0dff8080808080ffff01ff0bffff0101ff058080ff0180ff02ffff03ff0bffff01ff02ffff03ffff09ff23ff1880ffff01ff02ffff03ffff18ff81b3ff2c80ffff01ff02ffff03ffff20ff1780ffff01ff02ff3effff04ff02ffff04ff05ffff04ff1bffff04ff33ffff04ff2fffff04ff5fff8080808080808080ffff01ff088080ff0180ffff01ff04ff13ffff02ff3effff04ff02ffff04ff05ffff04ff1bffff04ff17ffff04ff2fffff04ff5fff80808080808080808080ff0180ffff01ff02ffff03ffff09ff23ffff0181e880ffff01ff02ff3effff04ff02ffff04ff05ffff04ff1bffff04ff17ffff04ffff02ffff03ffff22ffff09ffff02ff2effff04ff02ffff04ff53ff80808080ff82014f80ffff20ff5f8080ffff01ff02ff53ffff04ff818fffff04ff82014fffff04ff81b3ff8080808080ffff01ff088080ff0180ffff04ff2cff8080808080808080ffff01ff04ff13ffff02ff3effff04ff02ffff04ff05ffff04ff1bffff04ff17ffff04ff2fffff04ff5fff80808080808080808080ff018080ff0180ffff01ff04ffff04ff18ffff04ffff02ff16ffff04ff02ffff04ff05ffff04ff27ffff04ffff0bff2cff82014f80ffff04ffff02ff2effff04ff02ffff04ff818fff80808080ffff04ffff0bff2cff0580ff8080808080808080ff378080ff81af8080ff0180ff018080ffff04ffff01a0a04d9f57764f54a43e4030befb4d80026e870519aaa66334aef8304f5d0393c2ffff04ffff01ffa06661ea6604b491118b0f49c932c0f0de2ad815a57b54b6ec8fdbd1b408ae7e2780ffff04ffff01a057bfd1cb0adda3d94315053fda723f2028320faa8338225d99f629e3d46d43a9ffff04ffff01ff02ffff01ff02ffff01ff02ffff03ff0bffff01ff02ffff03ffff09ff05ffff1dff0bffff1effff0bff0bffff02ff06ffff04ff02ffff04ff17ff8080808080808080ffff01ff02ff17ff2f80ffff01ff088080ff0180ffff01ff04ffff04ff04ffff04ff05ffff04ffff02ff06ffff04ff02ffff04ff17ff80808080ff80808080ffff02ff17ff2f808080ff0180ffff04ffff01ff32ff02ffff03ffff07ff0580ffff01ff0bffff0102ffff02ff06ffff04ff02ffff04ff09ff80808080ffff02ff06ffff04ff02ffff04ff0dff8080808080ffff01ff0bffff0101ff058080ff0180ff018080ffff04ffff01b0a132fae32c98cbb7d8f5814c49ee3f0ba6ec2172c5e5f6900655a65cd2157a06a1c6eb89c68c8d2cdcee9506c2217978ff018080ff018080808080ff01808080ffffa0a14daf55d41ced6419bcd011fbc1f74ab9567fe55340d88435aa6493d628fa47ffa01804338c97f989c78d88716206c0f27315f3eb7d59417ab2eacee20f0a7ff60bff0180ff01ffffff80ffff02ffff01ff02ffff01ff02ffff03ff5fffff01ff02ff3affff04ff02ffff04ff0bffff04ff17ffff04ff2fffff04ff5fffff04ff81bfffff04ff82017fffff04ff8202ffffff04ffff02ff05ff8205ff80ff8080808080808080808080ffff01ff04ffff04ff10ffff01ff81ff8080ffff02ff05ff8205ff808080ff0180ffff04ffff01ffffff49ff3f02ff04ff0101ffff02ffff02ffff03ff05ffff01ff02ff2affff04ff02ffff04ff0dffff04ffff0bff12ffff0bff2cff1480ffff0bff12ffff0bff12ffff0bff2cff3c80ff0980ffff0bff12ff0bffff0bff2cff8080808080ff8080808080ffff010b80ff0180ff02ffff03ff05ffff01ff02ffff03ffff02ff3effff04ff02ffff04ff82011fffff04ff27ffff04ff4fff808080808080ffff01ff02ff3affff04ff02ffff04ff0dffff04ff1bffff04ff37ffff04ff6fffff04ff81dfffff04ff8201bfffff04ff82037fffff04ffff04ffff04ff28ffff04ffff0bffff02ff26ffff04ff02ffff04ff11ffff04ffff02ff26ffff04ff02ffff04ff13ffff04ff82027fffff04ffff02ff36ffff04ff02ffff04ff82013fff80808080ffff04ffff02ff36ffff04ff02ffff04ff819fff80808080ffff04ffff02ff36ffff04ff02ffff04ff13ff80808080ff8080808080808080ffff04ffff02ff36ffff04ff02ffff04ff09ff80808080ff808080808080ffff012480ff808080ff8202ff80ff8080808080808080808080ffff01ff088080ff0180ffff018202ff80ff0180ffffff0bff12ffff0bff2cff3880ffff0bff12ffff0bff12ffff0bff2cff3c80ff0580ffff0bff12ffff02ff2affff04ff02ffff04ff07ffff04ffff0bff2cff2c80ff8080808080ffff0bff2cff8080808080ff02ffff03ffff07ff0580ffff01ff0bffff0102ffff02ff36ffff04ff02ffff04ff09ff80808080ffff02ff36ffff04ff02ffff04ff0dff8080808080ffff01ff0bffff0101ff058080ff0180ffff02ffff03ff1bffff01ff02ff2effff04ff02ffff04ffff02ffff03ffff18ffff0101ff1380ffff01ff0bffff0102ff2bff0580ffff01ff0bffff0102ff05ff2b8080ff0180ffff04ffff04ffff17ff13ffff0181ff80ff3b80ff8080808080ffff010580ff0180ff02ffff03ff17ffff01ff02ffff03ffff09ff05ffff02ff2effff04ff02ffff04ff13ffff04ff27ff808080808080ffff01ff02ff3effff04ff02ffff04ff05ffff04ff1bffff04ff37ff808080808080ffff01ff088080ff0180ffff01ff010180ff0180ff018080ffff04ffff01ff01ffff81e8ff0bffffffffa0043fed6d67961e36db2900b6aab24aa68be529c4e632aace486fbea1b26dc70e80ffa057bfd1cb0adda3d94315053fda723f2028320faa8338225d99f629e3d46d43a980ff808080ffff33ffa09130956ec241f3c4f8807f6889e65025947fbd7bb757d8df0ba2640e293bcc60ff01ffffa0a14daf55d41ced6419bcd011fbc1f74ab9567fe55340d88435aa6493d628fa47ffa0043fed6d67961e36db2900b6aab24aa68be529c4e632aace486fbea1b26dc70effa0c842b1a384b8633ac25d0f12bd7b614f86a77642ab6426418750f2b0b86bab2a8080ffff3fffa0890bd0a05cc152c27a3a72348d59e9c5fb46c18da8f32948f1f2143b35014aca8080ffff04ffff01ffffa07faa3253bfddd1e0decb0906b2dc6247bbc4cf608f58345d173adb63e8b47c9fffa07acfcbd1ed73bfe2b698508f4ea5ed353c60ace154360272ce91f9ab0c8423c3a0eff07522495060c066f66f32acc2a77e3a3e737aca8baea4d1a64ea4cdc13da980ffff04ffff01ffa0a04d9f57764f54a43e4030befb4d80026e870519aaa66334aef8304f5d0393c280ffff04ffff01ffffa07f3e180acdf046f955d3440bb3a16dfd6f5a46c809cee98e7514127327b1cab58080ff018080808080ffff80ff80ff80ff80ff8080808080ca2e21c90d263e63b73d449a3f8d57b9458846f7af27d9a61a515395fa14071e69c05748eab24bce907ef7840c14873f1e668bda10c7aab57e25bb7895a88db20000000000000001ff02ffff01ff02ffff01ff02ffff03ffff18ff2fff3480ffff01ff04ffff04ff20ffff04ff2fff808080ffff04ffff02ff3effff04ff02ffff04ff05ffff04ffff02ff2affff04ff02ffff04ff27ffff04ffff02ffff03ff77ffff01ff02ff36ffff04ff02ffff04ff09ffff04ff57ffff04ffff02ff2effff04ff02ffff04ff05ff80808080ff808080808080ffff011d80ff0180ffff04ffff02ffff03ff77ffff0181b7ffff015780ff0180ff808080808080ffff04ff77ff808080808080ffff02ff3affff04ff02ffff04ff05ffff04ffff02ff0bff5f80ffff01ff8080808080808080ffff01ff088080ff0180ffff04ffff01ffffffff4947ff0233ffff0401ff0102ffffff20ff02ffff03ff05ffff01ff02ff32ffff04ff02ffff04ff0dffff04ffff0bff3cffff0bff34ff2480ffff0bff3cffff0bff3cffff0bff34ff2c80ff0980ffff0bff3cff0bffff0bff34ff8080808080ff8080808080ffff010b80ff0180ffff02ffff03ffff22ffff09ffff0dff0580ff2280ffff09ffff0dff0b80ff2280ffff15ff17ffff0181ff8080ffff01ff0bff05ff0bff1780ffff01ff088080ff0180ff02ffff03ff0bffff01ff02ffff03ffff02ff26ffff04ff02ffff04ff13ff80808080ffff01ff02ffff03ffff20ff1780ffff01ff02ffff03ffff09ff81b3ffff01818f80ffff01ff02ff3affff04ff02ffff04ff05ffff04ff1bffff04ff34ff808080808080ffff01ff04ffff04ff23ffff04ffff02ff36ffff04ff02ffff04ff09ffff04ff53ffff04ffff02ff2effff04ff02ffff04ff05ff80808080ff808080808080ff738080ffff02ff3affff04ff02ffff04ff05ffff04ff1bffff04ff34ff8080808080808080ff0180ffff01ff088080ff0180ffff01ff04ff13ffff02ff3affff04ff02ffff04ff05ffff04ff1bffff04ff17ff8080808080808080ff0180ffff01ff02ffff03ff17ff80ffff01ff088080ff018080ff0180ffffff02ffff03ffff09ff09ff3880ffff01ff02ffff03ffff18ff2dffff010180ffff01ff0101ff8080ff0180ff8080ff0180ff0bff3cffff0bff34ff2880ffff0bff3cffff0bff3cffff0bff34ff2c80ff0580ffff0bff3cffff02ff32ffff04ff02ffff04ff07ffff04ffff0bff34ff3480ff8080808080ffff0bff34ff8080808080ffff02ffff03ffff07ff0580ffff01ff0bffff0102ffff02ff2effff04ff02ffff04ff09ff80808080ffff02ff2effff04ff02ffff04ff0dff8080808080ffff01ff0bffff0101ff058080ff0180ff02ffff03ffff21ff17ffff09ff0bff158080ffff01ff04ff30ffff04ff0bff808080ffff01ff088080ff0180ff018080ffff04ffff01ffa07faa3253bfddd1e0decb0906b2dc6247bbc4cf608f58345d173adb63e8b47c9fffa0a14daf55d41ced6419bcd011fbc1f74ab9567fe55340d88435aa6493d628fa47a0eff07522495060c066f66f32acc2a77e3a3e737aca8baea4d1a64ea4cdc13da9ffff04ffff01ff02ffff01ff02ffff01ff02ff3effff04ff02ffff04ff05ffff04ffff02ff2fff5f80ffff04ff80ffff04ffff04ffff04ff0bffff04ff17ff808080ffff01ff808080ffff01ff8080808080808080ffff04ffff01ffffff0233ff04ff0101ffff02ff02ffff03ff05ffff01ff02ff1affff04ff02ffff04ff0dffff04ffff0bff12ffff0bff2cff1480ffff0bff12ffff0bff12ffff0bff2cff3c80ff0980ffff0bff12ff0bffff0bff2cff8080808080ff8080808080ffff010b80ff0180ffff0bff12ffff0bff2cff1080ffff0bff12ffff0bff12ffff0bff2cff3c80ff0580ffff0bff12ffff02ff1affff04ff02ffff04ff07ffff04ffff0bff2cff2c80ff8080808080ffff0bff2cff8080808080ffff02ffff03ffff07ff0580ffff01ff0bffff0102ffff02ff2effff04ff02ffff04ff09ff80808080ffff02ff2effff04ff02ffff04ff0dff8080808080ffff01ff0bffff0101ff058080ff0180ff02ffff03ff0bffff01ff02ffff03ffff09ff23ff1880ffff01ff02ffff03ffff18ff81b3ff2c80ffff01ff02ffff03ffff20ff1780ffff01ff02ff3effff04ff02ffff04ff05ffff04ff1bffff04ff33ffff04ff2fffff04ff5fff8080808080808080ffff01ff088080ff0180ffff01ff04ff13ffff02ff3effff04ff02ffff04ff05ffff04ff1bffff04ff17ffff04ff2fffff04ff5fff80808080808080808080ff0180ffff01ff02ffff03ffff09ff23ffff0181e880ffff01ff02ff3effff04ff02ffff04ff05ffff04ff1bffff04ff17ffff04ffff02ffff03ffff22ffff09ffff02ff2effff04ff02ffff04ff53ff80808080ff82014f80ffff20ff5f8080ffff01ff02ff53ffff04ff818fffff04ff82014fffff04ff81b3ff8080808080ffff01ff088080ff0180ffff04ff2cff8080808080808080ffff01ff04ff13ffff02ff3effff04ff02ffff04ff05ffff04ff1bffff04ff17ffff04ff2fffff04ff5fff80808080808080808080ff018080ff0180ffff01ff04ffff04ff18ffff04ffff02ff16ffff04ff02ffff04ff05ffff04ff27ffff04ffff0bff2cff82014f80ffff04ffff02ff2effff04ff02ffff04ff818fff80808080ffff04ffff0bff2cff0580ff8080808080808080ff378080ff81af8080ff0180ff018080ffff04ffff01a0a04d9f57764f54a43e4030befb4d80026e870519aaa66334aef8304f5d0393c2ffff04ffff01ffa0043fed6d67961e36db2900b6aab24aa68be529c4e632aace486fbea1b26dc70e80ffff04ffff01a057bfd1cb0adda3d94315053fda723f2028320faa8338225d99f629e3d46d43a9ffff04ffff01ff01ffff33ffa0c842b1a384b8633ac25d0f12bd7b614f86a77642ab6426418750f2b0b86bab2aff01ffffa0a14daf55d41ced6419bcd011fbc1f74ab9567fe55340d88435aa6493d628fa47ffa0043fed6d67961e36db2900b6aab24aa68be529c4e632aace486fbea1b26dc70effa0c842b1a384b8633ac25d0f12bd7b614f86a77642ab6426418750f2b0b86bab2a8080ffff3eff248080ff018080808080ff01808080ffffa032dbe6d545f24635c7871ea53c623c358d7cea8f5e27a983ba6e5c0bf35fa243ffa08c4aebb18e8ce08405083c3d90a29f30239865142e2dcbca5393f40df9e3821dff0180ff01ffff808080b7e7c50689b473f769bb4c03fc58350b30569a93d34a99ba9636a49f2cdbe969c889452b8b955908da832dfb9fe7b1ba0c0209e3312368eb05bc7c8582ef9886f440c78cef8c332bbf9f84f40b00fbaa3aa4759bbf484c74b6eeaac4be952af5",  # noqa: E501
        "taker": [
            {
                "store_id": "7acfcbd1ed73bfe2b698508f4ea5ed353c60ace154360272ce91f9ab0c8423c3",
                "inclusions": [{"key": "10", "value": "0210"}],
            }
        ],
        "maker": [
            {
                "store_id": "a14daf55d41ced6419bcd011fbc1f74ab9567fe55340d88435aa6493d628fa47",
                "proofs": [
                    {
                        "key": "10",
                        "value": "0110",
                        "node_hash": "de4ec93c032f5117d8af076dfc86faa5987a6c0b1d52ffc9cf0dfa43989d8c58",
                        "layers": [
                            {
                                "other_hash_side": "left",
                                "other_hash": "1c8ab812b97f5a9da0ba4be2380104810fe5c8022efe9b9e2c9d188fc3537434",
                                "combined_hash": "d340000b3a6717a5a8d42b24516ff69430235c771f8a527554b357b7f03c6de0",
                            },
                            {
                                "other_hash_side": "right",
                                "other_hash": "54e8b4cac761778f396840b343c0f1cb0e1fd0c9927d48d2f0d09a7a6f225126",
                                "combined_hash": "7676004a15439e4e8345d0f9f3a15500805b3447285904b7bcd7d14e27381d2e",
                            },
                            {
                                "other_hash_side": "right",
                                "other_hash": "e24b5bf6fa30a0fb836b369a471c957afcf8c2c39521f9ffd0b45aa9f172e8b9",
                                "combined_hash": "cf98873e50b9e84485c5b6729b6023e24140a7c019efe06ee594256e8f8bf523",
                            },
                            {
                                "other_hash_side": "right",
                                "other_hash": "bcff6f16886339a196a2f6c842ad6d350a8579d123eb8602a0a85965ba25d671",
                                "combined_hash": "043fed6d67961e36db2900b6aab24aa68be529c4e632aace486fbea1b26dc70e",
                            },
                        ],
                    },
                    {
                        "key": "11",
                        "value": "0111",
                        "node_hash": "e866daa84d1785d1e1e3b228e2fd50031342e7501c08a074965da3d4f5ca4be2",
                        "layers": [
                            {
                                "other_hash_side": "left",
                                "other_hash": "9daec46b6819e836d66144119dd084765cfe7ed9ac3222c0c0f64590a4a43b3a",
                                "combined_hash": "dfa8a2f284a05d6974096f138ec2a66086065ec2fbec7e564b367bb15e81d75d",
                            },
                            {
                                "other_hash_side": "right",
                                "other_hash": "9e4574191777193c145c7e09eb6394501f81dee6eb1b05f0881bb478828cb9ea",
                                "combined_hash": "e24b5bf6fa30a0fb836b369a471c957afcf8c2c39521f9ffd0b45aa9f172e8b9",
                            },
                            {
                                "other_hash_side": "left",
                                "other_hash": "7676004a15439e4e8345d0f9f3a15500805b3447285904b7bcd7d14e27381d2e",
                                "combined_hash": "cf98873e50b9e84485c5b6729b6023e24140a7c019efe06ee594256e8f8bf523",
                            },
                            {
                                "other_hash_side": "right",
                                "other_hash": "bcff6f16886339a196a2f6c842ad6d350a8579d123eb8602a0a85965ba25d671",
                                "combined_hash": "043fed6d67961e36db2900b6aab24aa68be529c4e632aace486fbea1b26dc70e",
                            },
                        ],
                    },
                ],
            }
        ],
    },
    maker_inclusions=[
        {"key": b"\x10".hex(), "value": b"\x01\x10".hex()},
        {"key": b"\x11".hex(), "value": b"\x01\x11".hex()},
    ],
    taker_inclusions=[{"key": b"\x10".hex(), "value": b"\x02\x10".hex()}],
    trade_id="949a35ec88a2731874097d2a53aaae2f2f665172d668e466415e39d1ad56ea61",
    maker_root_history=[
        bytes32.from_hexstr("6661ea6604b491118b0f49c932c0f0de2ad815a57b54b6ec8fdbd1b408ae7e27"),
        bytes32.from_hexstr("043fed6d67961e36db2900b6aab24aa68be529c4e632aace486fbea1b26dc70e"),
    ],
    taker_root_history=[
        bytes32.from_hexstr("42f08ebc0578f2cec7a9ad1c3038e74e0f30eba5c2f4cb1ee1c8fdb682c19dbb"),
        bytes32.from_hexstr("eeb63ac765065d2ee161e1c059c8188ef809e1c3ed8739bad5bfee2c2ee1c742"),
    ],
)


make_one_take_two_reference = MakeAndTakeReference(
    entries_to_insert=10,
    make_offer_response={
        "trade_id": "aac7393bf6299a9f519905b304d71acbabce1588dd82cad2f45c727f00a9161f",
        "offer": "0000000300000000000000000000000000000000000000000000000000000000000000002a5e6ce75ee92c329c576b5dfe550bd8a9054a7b3da5c5f74b1c2c55417fc05f0000000000000000ff02ffff01ff02ffff01ff02ffff03ffff18ff2fff3480ffff01ff04ffff04ff20ffff04ff2fff808080ffff04ffff02ff3effff04ff02ffff04ff05ffff04ffff02ff2affff04ff02ffff04ff27ffff04ffff02ffff03ff77ffff01ff02ff36ffff04ff02ffff04ff09ffff04ff57ffff04ffff02ff2effff04ff02ffff04ff05ff80808080ff808080808080ffff011d80ff0180ffff04ffff02ffff03ff77ffff0181b7ffff015780ff0180ff808080808080ffff04ff77ff808080808080ffff02ff3affff04ff02ffff04ff05ffff04ffff02ff0bff5f80ffff01ff8080808080808080ffff01ff088080ff0180ffff04ffff01ffffffff4947ff0233ffff0401ff0102ffffff20ff02ffff03ff05ffff01ff02ff32ffff04ff02ffff04ff0dffff04ffff0bff3cffff0bff34ff2480ffff0bff3cffff0bff3cffff0bff34ff2c80ff0980ffff0bff3cff0bffff0bff34ff8080808080ff8080808080ffff010b80ff0180ffff02ffff03ffff22ffff09ffff0dff0580ff2280ffff09ffff0dff0b80ff2280ffff15ff17ffff0181ff8080ffff01ff0bff05ff0bff1780ffff01ff088080ff0180ff02ffff03ff0bffff01ff02ffff03ffff02ff26ffff04ff02ffff04ff13ff80808080ffff01ff02ffff03ffff20ff1780ffff01ff02ffff03ffff09ff81b3ffff01818f80ffff01ff02ff3affff04ff02ffff04ff05ffff04ff1bffff04ff34ff808080808080ffff01ff04ffff04ff23ffff04ffff02ff36ffff04ff02ffff04ff09ffff04ff53ffff04ffff02ff2effff04ff02ffff04ff05ff80808080ff808080808080ff738080ffff02ff3affff04ff02ffff04ff05ffff04ff1bffff04ff34ff8080808080808080ff0180ffff01ff088080ff0180ffff01ff04ff13ffff02ff3affff04ff02ffff04ff05ffff04ff1bffff04ff17ff8080808080808080ff0180ffff01ff02ffff03ff17ff80ffff01ff088080ff018080ff0180ffffff02ffff03ffff09ff09ff3880ffff01ff02ffff03ffff18ff2dffff010180ffff01ff0101ff8080ff0180ff8080ff0180ff0bff3cffff0bff34ff2880ffff0bff3cffff0bff3cffff0bff34ff2c80ff0580ffff0bff3cffff02ff32ffff04ff02ffff04ff07ffff04ffff0bff34ff3480ff8080808080ffff0bff34ff8080808080ffff02ffff03ffff07ff0580ffff01ff0bffff0102ffff02ff2effff04ff02ffff04ff09ff80808080ffff02ff2effff04ff02ffff04ff0dff8080808080ffff01ff0bffff0101ff058080ff0180ff02ffff03ffff21ff17ffff09ff0bff158080ffff01ff04ff30ffff04ff0bff808080ffff01ff088080ff0180ff018080ffff04ffff01ffa07faa3253bfddd1e0decb0906b2dc6247bbc4cf608f58345d173adb63e8b47c9fffa07acfcbd1ed73bfe2b698508f4ea5ed353c60ace154360272ce91f9ab0c8423c3a0eff07522495060c066f66f32acc2a77e3a3e737aca8baea4d1a64ea4cdc13da9ffff04ffff01ff02ffff01ff02ffff01ff02ff3effff04ff02ffff04ff05ffff04ffff02ff2fff5f80ffff04ff80ffff04ffff04ffff04ff0bffff04ff17ff808080ffff01ff808080ffff01ff8080808080808080ffff04ffff01ffffff0233ff04ff0101ffff02ff02ffff03ff05ffff01ff02ff1affff04ff02ffff04ff0dffff04ffff0bff12ffff0bff2cff1480ffff0bff12ffff0bff12ffff0bff2cff3c80ff0980ffff0bff12ff0bffff0bff2cff8080808080ff8080808080ffff010b80ff0180ffff0bff12ffff0bff2cff1080ffff0bff12ffff0bff12ffff0bff2cff3c80ff0580ffff0bff12ffff02ff1affff04ff02ffff04ff07ffff04ffff0bff2cff2c80ff8080808080ffff0bff2cff8080808080ffff02ffff03ffff07ff0580ffff01ff0bffff0102ffff02ff2effff04ff02ffff04ff09ff80808080ffff02ff2effff04ff02ffff04ff0dff8080808080ffff01ff0bffff0101ff058080ff0180ff02ffff03ff0bffff01ff02ffff03ffff09ff23ff1880ffff01ff02ffff03ffff18ff81b3ff2c80ffff01ff02ffff03ffff20ff1780ffff01ff02ff3effff04ff02ffff04ff05ffff04ff1bffff04ff33ffff04ff2fffff04ff5fff8080808080808080ffff01ff088080ff0180ffff01ff04ff13ffff02ff3effff04ff02ffff04ff05ffff04ff1bffff04ff17ffff04ff2fffff04ff5fff80808080808080808080ff0180ffff01ff02ffff03ffff09ff23ffff0181e880ffff01ff02ff3effff04ff02ffff04ff05ffff04ff1bffff04ff17ffff04ffff02ffff03ffff22ffff09ffff02ff2effff04ff02ffff04ff53ff80808080ff82014f80ffff20ff5f8080ffff01ff02ff53ffff04ff818fffff04ff82014fffff04ff81b3ff8080808080ffff01ff088080ff0180ffff04ff2cff8080808080808080ffff01ff04ff13ffff02ff3effff04ff02ffff04ff05ffff04ff1bffff04ff17ffff04ff2fffff04ff5fff80808080808080808080ff018080ff0180ffff01ff04ffff04ff18ffff04ffff02ff16ffff04ff02ffff04ff05ffff04ff27ffff04ffff0bff2cff82014f80ffff04ffff02ff2effff04ff02ffff04ff818fff80808080ffff04ffff0bff2cff0580ff8080808080808080ff378080ff81af8080ff0180ff018080ffff04ffff01a0a04d9f57764f54a43e4030befb4d80026e870519aaa66334aef8304f5d0393c2ffff04ffff01ffa042f08ebc0578f2cec7a9ad1c3038e74e0f30eba5c2f4cb1ee1c8fdb682c19dbb80ffff04ffff01a057bfd1cb0adda3d94315053fda723f2028320faa8338225d99f629e3d46d43a9ffff04ffff01ff02ffff01ff02ff0affff04ff02ffff04ff03ff80808080ffff04ffff01ffff333effff02ffff03ff05ffff01ff04ffff04ff0cffff04ffff02ff1effff04ff02ffff04ff09ff80808080ff808080ffff02ff16ffff04ff02ffff04ff19ffff04ffff02ff0affff04ff02ffff04ff0dff80808080ff808080808080ff8080ff0180ffff02ffff03ff05ffff01ff04ffff04ff08ff0980ffff02ff16ffff04ff02ffff04ff0dffff04ff0bff808080808080ffff010b80ff0180ff02ffff03ffff07ff0580ffff01ff0bffff0102ffff02ff1effff04ff02ffff04ff09ff80808080ffff02ff1effff04ff02ffff04ff0dff8080808080ffff01ff0bffff0101ff058080ff0180ff018080ff018080808080ff01808080ffffa00000000000000000000000000000000000000000000000000000000000000000ffffa00000000000000000000000000000000000000000000000000000000000000000ff01ff8080808032dbe6d545f24635c7871ea53c623c358d7cea8f5e27a983ba6e5c0bf35fa243aa064e96a86637d8f5ebe153dc8645d29f43bee762d5ec10d06c8617fa60b8c50000000000000001ff02ffff01ff02ffff01ff02ffff03ffff18ff2fff3480ffff01ff04ffff04ff20ffff04ff2fff808080ffff04ffff02ff3effff04ff02ffff04ff05ffff04ffff02ff2affff04ff02ffff04ff27ffff04ffff02ffff03ff77ffff01ff02ff36ffff04ff02ffff04ff09ffff04ff57ffff04ffff02ff2effff04ff02ffff04ff05ff80808080ff808080808080ffff011d80ff0180ffff04ffff02ffff03ff77ffff0181b7ffff015780ff0180ff808080808080ffff04ff77ff808080808080ffff02ff3affff04ff02ffff04ff05ffff04ffff02ff0bff5f80ffff01ff8080808080808080ffff01ff088080ff0180ffff04ffff01ffffffff4947ff0233ffff0401ff0102ffffff20ff02ffff03ff05ffff01ff02ff32ffff04ff02ffff04ff0dffff04ffff0bff3cffff0bff34ff2480ffff0bff3cffff0bff3cffff0bff34ff2c80ff0980ffff0bff3cff0bffff0bff34ff8080808080ff8080808080ffff010b80ff0180ffff02ffff03ffff22ffff09ffff0dff0580ff2280ffff09ffff0dff0b80ff2280ffff15ff17ffff0181ff8080ffff01ff0bff05ff0bff1780ffff01ff088080ff0180ff02ffff03ff0bffff01ff02ffff03ffff02ff26ffff04ff02ffff04ff13ff80808080ffff01ff02ffff03ffff20ff1780ffff01ff02ffff03ffff09ff81b3ffff01818f80ffff01ff02ff3affff04ff02ffff04ff05ffff04ff1bffff04ff34ff808080808080ffff01ff04ffff04ff23ffff04ffff02ff36ffff04ff02ffff04ff09ffff04ff53ffff04ffff02ff2effff04ff02ffff04ff05ff80808080ff808080808080ff738080ffff02ff3affff04ff02ffff04ff05ffff04ff1bffff04ff34ff8080808080808080ff0180ffff01ff088080ff0180ffff01ff04ff13ffff02ff3affff04ff02ffff04ff05ffff04ff1bffff04ff17ff8080808080808080ff0180ffff01ff02ffff03ff17ff80ffff01ff088080ff018080ff0180ffffff02ffff03ffff09ff09ff3880ffff01ff02ffff03ffff18ff2dffff010180ffff01ff0101ff8080ff0180ff8080ff0180ff0bff3cffff0bff34ff2880ffff0bff3cffff0bff3cffff0bff34ff2c80ff0580ffff0bff3cffff02ff32ffff04ff02ffff04ff07ffff04ffff0bff34ff3480ff8080808080ffff0bff34ff8080808080ffff02ffff03ffff07ff0580ffff01ff0bffff0102ffff02ff2effff04ff02ffff04ff09ff80808080ffff02ff2effff04ff02ffff04ff0dff8080808080ffff01ff0bffff0101ff058080ff0180ff02ffff03ffff21ff17ffff09ff0bff158080ffff01ff04ff30ffff04ff0bff808080ffff01ff088080ff0180ff018080ffff04ffff01ffa07faa3253bfddd1e0decb0906b2dc6247bbc4cf608f58345d173adb63e8b47c9fffa0a14daf55d41ced6419bcd011fbc1f74ab9567fe55340d88435aa6493d628fa47a0eff07522495060c066f66f32acc2a77e3a3e737aca8baea4d1a64ea4cdc13da9ffff04ffff01ff02ffff01ff02ffff01ff02ff3effff04ff02ffff04ff05ffff04ffff02ff2fff5f80ffff04ff80ffff04ffff04ffff04ff0bffff04ff17ff808080ffff01ff808080ffff01ff8080808080808080ffff04ffff01ffffff0233ff04ff0101ffff02ff02ffff03ff05ffff01ff02ff1affff04ff02ffff04ff0dffff04ffff0bff12ffff0bff2cff1480ffff0bff12ffff0bff12ffff0bff2cff3c80ff0980ffff0bff12ff0bffff0bff2cff8080808080ff8080808080ffff010b80ff0180ffff0bff12ffff0bff2cff1080ffff0bff12ffff0bff12ffff0bff2cff3c80ff0580ffff0bff12ffff02ff1affff04ff02ffff04ff07ffff04ffff0bff2cff2c80ff8080808080ffff0bff2cff8080808080ffff02ffff03ffff07ff0580ffff01ff0bffff0102ffff02ff2effff04ff02ffff04ff09ff80808080ffff02ff2effff04ff02ffff04ff0dff8080808080ffff01ff0bffff0101ff058080ff0180ff02ffff03ff0bffff01ff02ffff03ffff09ff23ff1880ffff01ff02ffff03ffff18ff81b3ff2c80ffff01ff02ffff03ffff20ff1780ffff01ff02ff3effff04ff02ffff04ff05ffff04ff1bffff04ff33ffff04ff2fffff04ff5fff8080808080808080ffff01ff088080ff0180ffff01ff04ff13ffff02ff3effff04ff02ffff04ff05ffff04ff1bffff04ff17ffff04ff2fffff04ff5fff80808080808080808080ff0180ffff01ff02ffff03ffff09ff23ffff0181e880ffff01ff02ff3effff04ff02ffff04ff05ffff04ff1bffff04ff17ffff04ffff02ffff03ffff22ffff09ffff02ff2effff04ff02ffff04ff53ff80808080ff82014f80ffff20ff5f8080ffff01ff02ff53ffff04ff818fffff04ff82014fffff04ff81b3ff8080808080ffff01ff088080ff0180ffff04ff2cff8080808080808080ffff01ff04ff13ffff02ff3effff04ff02ffff04ff05ffff04ff1bffff04ff17ffff04ff2fffff04ff5fff80808080808080808080ff018080ff0180ffff01ff04ffff04ff18ffff04ffff02ff16ffff04ff02ffff04ff05ffff04ff27ffff04ffff0bff2cff82014f80ffff04ffff02ff2effff04ff02ffff04ff818fff80808080ffff04ffff0bff2cff0580ff8080808080808080ff378080ff81af8080ff0180ff018080ffff04ffff01a0a04d9f57764f54a43e4030befb4d80026e870519aaa66334aef8304f5d0393c2ffff04ffff01ffa06661ea6604b491118b0f49c932c0f0de2ad815a57b54b6ec8fdbd1b408ae7e2780ffff04ffff01a057bfd1cb0adda3d94315053fda723f2028320faa8338225d99f629e3d46d43a9ffff04ffff01ff02ffff01ff02ffff01ff02ffff03ff0bffff01ff02ffff03ffff09ff05ffff1dff0bffff1effff0bff0bffff02ff06ffff04ff02ffff04ff17ff8080808080808080ffff01ff02ff17ff2f80ffff01ff088080ff0180ffff01ff04ffff04ff04ffff04ff05ffff04ffff02ff06ffff04ff02ffff04ff17ff80808080ff80808080ffff02ff17ff2f808080ff0180ffff04ffff01ff32ff02ffff03ffff07ff0580ffff01ff0bffff0102ffff02ff06ffff04ff02ffff04ff09ff80808080ffff02ff06ffff04ff02ffff04ff0dff8080808080ffff01ff0bffff0101ff058080ff0180ff018080ffff04ffff01b0a132fae32c98cbb7d8f5814c49ee3f0ba6ec2172c5e5f6900655a65cd2157a06a1c6eb89c68c8d2cdcee9506c2217978ff018080ff018080808080ff01808080ffffa0a14daf55d41ced6419bcd011fbc1f74ab9567fe55340d88435aa6493d628fa47ffa01804338c97f989c78d88716206c0f27315f3eb7d59417ab2eacee20f0a7ff60bff0180ff01ffffff80ffff02ffff01ff02ffff01ff02ffff03ff5fffff01ff02ff3affff04ff02ffff04ff0bffff04ff17ffff04ff2fffff04ff5fffff04ff81bfffff04ff82017fffff04ff8202ffffff04ffff02ff05ff8205ff80ff8080808080808080808080ffff01ff04ffff04ff10ffff01ff81ff8080ffff02ff05ff8205ff808080ff0180ffff04ffff01ffffff49ff3f02ff04ff0101ffff02ffff02ffff03ff05ffff01ff02ff2affff04ff02ffff04ff0dffff04ffff0bff12ffff0bff2cff1480ffff0bff12ffff0bff12ffff0bff2cff3c80ff0980ffff0bff12ff0bffff0bff2cff8080808080ff8080808080ffff010b80ff0180ff02ffff03ff05ffff01ff02ffff03ffff02ff3effff04ff02ffff04ff82011fffff04ff27ffff04ff4fff808080808080ffff01ff02ff3affff04ff02ffff04ff0dffff04ff1bffff04ff37ffff04ff6fffff04ff81dfffff04ff8201bfffff04ff82037fffff04ffff04ffff04ff28ffff04ffff0bffff02ff26ffff04ff02ffff04ff11ffff04ffff02ff26ffff04ff02ffff04ff13ffff04ff82027fffff04ffff02ff36ffff04ff02ffff04ff82013fff80808080ffff04ffff02ff36ffff04ff02ffff04ff819fff80808080ffff04ffff02ff36ffff04ff02ffff04ff13ff80808080ff8080808080808080ffff04ffff02ff36ffff04ff02ffff04ff09ff80808080ff808080808080ffff012480ff808080ff8202ff80ff8080808080808080808080ffff01ff088080ff0180ffff018202ff80ff0180ffffff0bff12ffff0bff2cff3880ffff0bff12ffff0bff12ffff0bff2cff3c80ff0580ffff0bff12ffff02ff2affff04ff02ffff04ff07ffff04ffff0bff2cff2c80ff8080808080ffff0bff2cff8080808080ff02ffff03ffff07ff0580ffff01ff0bffff0102ffff02ff36ffff04ff02ffff04ff09ff80808080ffff02ff36ffff04ff02ffff04ff0dff8080808080ffff01ff0bffff0101ff058080ff0180ffff02ffff03ff1bffff01ff02ff2effff04ff02ffff04ffff02ffff03ffff18ffff0101ff1380ffff01ff0bffff0102ff2bff0580ffff01ff0bffff0102ff05ff2b8080ff0180ffff04ffff04ffff17ff13ffff0181ff80ff3b80ff8080808080ffff010580ff0180ff02ffff03ff17ffff01ff02ffff03ffff09ff05ffff02ff2effff04ff02ffff04ff13ffff04ff27ff808080808080ffff01ff02ff3effff04ff02ffff04ff05ffff04ff1bffff04ff37ff808080808080ffff01ff088080ff0180ffff01ff010180ff0180ff018080ffff04ffff01ff01ffff81e8ff0bffffffffa08e54f5066aa7999fc1561a56df59d11ff01f7df93cadf49a61adebf65dec65ea80ffa057bfd1cb0adda3d94315053fda723f2028320faa8338225d99f629e3d46d43a980ff808080ffff33ffa0ca77e42ac3b3375edc54af271f21d075afd02d72969cababeec63e22f7ab10deff01ffffa0a14daf55d41ced6419bcd011fbc1f74ab9567fe55340d88435aa6493d628fa47ffa08e54f5066aa7999fc1561a56df59d11ff01f7df93cadf49a61adebf65dec65eaffa0c842b1a384b8633ac25d0f12bd7b614f86a77642ab6426418750f2b0b86bab2a8080ffff3fffa0bd7aa54c5f93ef1738439aa60b471ce2aa4c62fb18a7943aa10061f00dbdb8368080ffff04ffff01ffffa07faa3253bfddd1e0decb0906b2dc6247bbc4cf608f58345d173adb63e8b47c9fffa07acfcbd1ed73bfe2b698508f4ea5ed353c60ace154360272ce91f9ab0c8423c3a0eff07522495060c066f66f32acc2a77e3a3e737aca8baea4d1a64ea4cdc13da980ffff04ffff01ffa0a04d9f57764f54a43e4030befb4d80026e870519aaa66334aef8304f5d0393c280ffff04ffff01ffffa07f3e180acdf046f955d3440bb3a16dfd6f5a46c809cee98e7514127327b1cab5ffa05eadd0f5982411ec074786cb6e2e37880d2ea1f007b47bc50a1b36cc2c61ba098080ff018080808080ffff80ff80ff80ff80ff8080808080ca2e21c90d263e63b73d449a3f8d57b9458846f7af27d9a61a515395fa14071ea55bba78c76265b4bac257251b1e89dd13637a7c18e8dcb03e092dfb7eb5a84a0000000000000001ff02ffff01ff02ffff01ff02ffff03ffff18ff2fff3480ffff01ff04ffff04ff20ffff04ff2fff808080ffff04ffff02ff3effff04ff02ffff04ff05ffff04ffff02ff2affff04ff02ffff04ff27ffff04ffff02ffff03ff77ffff01ff02ff36ffff04ff02ffff04ff09ffff04ff57ffff04ffff02ff2effff04ff02ffff04ff05ff80808080ff808080808080ffff011d80ff0180ffff04ffff02ffff03ff77ffff0181b7ffff015780ff0180ff808080808080ffff04ff77ff808080808080ffff02ff3affff04ff02ffff04ff05ffff04ffff02ff0bff5f80ffff01ff8080808080808080ffff01ff088080ff0180ffff04ffff01ffffffff4947ff0233ffff0401ff0102ffffff20ff02ffff03ff05ffff01ff02ff32ffff04ff02ffff04ff0dffff04ffff0bff3cffff0bff34ff2480ffff0bff3cffff0bff3cffff0bff34ff2c80ff0980ffff0bff3cff0bffff0bff34ff8080808080ff8080808080ffff010b80ff0180ffff02ffff03ffff22ffff09ffff0dff0580ff2280ffff09ffff0dff0b80ff2280ffff15ff17ffff0181ff8080ffff01ff0bff05ff0bff1780ffff01ff088080ff0180ff02ffff03ff0bffff01ff02ffff03ffff02ff26ffff04ff02ffff04ff13ff80808080ffff01ff02ffff03ffff20ff1780ffff01ff02ffff03ffff09ff81b3ffff01818f80ffff01ff02ff3affff04ff02ffff04ff05ffff04ff1bffff04ff34ff808080808080ffff01ff04ffff04ff23ffff04ffff02ff36ffff04ff02ffff04ff09ffff04ff53ffff04ffff02ff2effff04ff02ffff04ff05ff80808080ff808080808080ff738080ffff02ff3affff04ff02ffff04ff05ffff04ff1bffff04ff34ff8080808080808080ff0180ffff01ff088080ff0180ffff01ff04ff13ffff02ff3affff04ff02ffff04ff05ffff04ff1bffff04ff17ff8080808080808080ff0180ffff01ff02ffff03ff17ff80ffff01ff088080ff018080ff0180ffffff02ffff03ffff09ff09ff3880ffff01ff02ffff03ffff18ff2dffff010180ffff01ff0101ff8080ff0180ff8080ff0180ff0bff3cffff0bff34ff2880ffff0bff3cffff0bff3cffff0bff34ff2c80ff0580ffff0bff3cffff02ff32ffff04ff02ffff04ff07ffff04ffff0bff34ff3480ff8080808080ffff0bff34ff8080808080ffff02ffff03ffff07ff0580ffff01ff0bffff0102ffff02ff2effff04ff02ffff04ff09ff80808080ffff02ff2effff04ff02ffff04ff0dff8080808080ffff01ff0bffff0101ff058080ff0180ff02ffff03ffff21ff17ffff09ff0bff158080ffff01ff04ff30ffff04ff0bff808080ffff01ff088080ff0180ff018080ffff04ffff01ffa07faa3253bfddd1e0decb0906b2dc6247bbc4cf608f58345d173adb63e8b47c9fffa0a14daf55d41ced6419bcd011fbc1f74ab9567fe55340d88435aa6493d628fa47a0eff07522495060c066f66f32acc2a77e3a3e737aca8baea4d1a64ea4cdc13da9ffff04ffff01ff02ffff01ff02ffff01ff02ff3effff04ff02ffff04ff05ffff04ffff02ff2fff5f80ffff04ff80ffff04ffff04ffff04ff0bffff04ff17ff808080ffff01ff808080ffff01ff8080808080808080ffff04ffff01ffffff0233ff04ff0101ffff02ff02ffff03ff05ffff01ff02ff1affff04ff02ffff04ff0dffff04ffff0bff12ffff0bff2cff1480ffff0bff12ffff0bff12ffff0bff2cff3c80ff0980ffff0bff12ff0bffff0bff2cff8080808080ff8080808080ffff010b80ff0180ffff0bff12ffff0bff2cff1080ffff0bff12ffff0bff12ffff0bff2cff3c80ff0580ffff0bff12ffff02ff1affff04ff02ffff04ff07ffff04ffff0bff2cff2c80ff8080808080ffff0bff2cff8080808080ffff02ffff03ffff07ff0580ffff01ff0bffff0102ffff02ff2effff04ff02ffff04ff09ff80808080ffff02ff2effff04ff02ffff04ff0dff8080808080ffff01ff0bffff0101ff058080ff0180ff02ffff03ff0bffff01ff02ffff03ffff09ff23ff1880ffff01ff02ffff03ffff18ff81b3ff2c80ffff01ff02ffff03ffff20ff1780ffff01ff02ff3effff04ff02ffff04ff05ffff04ff1bffff04ff33ffff04ff2fffff04ff5fff8080808080808080ffff01ff088080ff0180ffff01ff04ff13ffff02ff3effff04ff02ffff04ff05ffff04ff1bffff04ff17ffff04ff2fffff04ff5fff80808080808080808080ff0180ffff01ff02ffff03ffff09ff23ffff0181e880ffff01ff02ff3effff04ff02ffff04ff05ffff04ff1bffff04ff17ffff04ffff02ffff03ffff22ffff09ffff02ff2effff04ff02ffff04ff53ff80808080ff82014f80ffff20ff5f8080ffff01ff02ff53ffff04ff818fffff04ff82014fffff04ff81b3ff8080808080ffff01ff088080ff0180ffff04ff2cff8080808080808080ffff01ff04ff13ffff02ff3effff04ff02ffff04ff05ffff04ff1bffff04ff17ffff04ff2fffff04ff5fff80808080808080808080ff018080ff0180ffff01ff04ffff04ff18ffff04ffff02ff16ffff04ff02ffff04ff05ffff04ff27ffff04ffff0bff2cff82014f80ffff04ffff02ff2effff04ff02ffff04ff818fff80808080ffff04ffff0bff2cff0580ff8080808080808080ff378080ff81af8080ff0180ff018080ffff04ffff01a0a04d9f57764f54a43e4030befb4d80026e870519aaa66334aef8304f5d0393c2ffff04ffff01ffa08e54f5066aa7999fc1561a56df59d11ff01f7df93cadf49a61adebf65dec65ea80ffff04ffff01a057bfd1cb0adda3d94315053fda723f2028320faa8338225d99f629e3d46d43a9ffff04ffff01ff01ffff33ffa0c842b1a384b8633ac25d0f12bd7b614f86a77642ab6426418750f2b0b86bab2aff01ffffa0a14daf55d41ced6419bcd011fbc1f74ab9567fe55340d88435aa6493d628fa47ffa08e54f5066aa7999fc1561a56df59d11ff01f7df93cadf49a61adebf65dec65eaffa0c842b1a384b8633ac25d0f12bd7b614f86a77642ab6426418750f2b0b86bab2a8080ffff3eff248080ff018080808080ff01808080ffffa032dbe6d545f24635c7871ea53c623c358d7cea8f5e27a983ba6e5c0bf35fa243ffa08c4aebb18e8ce08405083c3d90a29f30239865142e2dcbca5393f40df9e3821dff0180ff01ffff808080af0175e92a6d239da94fd772d57303bcf7bc34eb1001bffce6d4b12a271eeb51368883b184e5a7b26a806f632483e13e0d5858ee77d06fc8d91be31b8c98dc7fd63dfee3812ace6495915a0880c2b37e48ff461cce9df881717a91d5d59d069e",  # noqa: E501
        "taker": [
            {
                "store_id": "7acfcbd1ed73bfe2b698508f4ea5ed353c60ace154360272ce91f9ab0c8423c3",
                "inclusions": [{"key": "10", "value": "0210"}, {"key": "11", "value": "0211"}],
            }
        ],
        "maker": [
            {
                "store_id": "a14daf55d41ced6419bcd011fbc1f74ab9567fe55340d88435aa6493d628fa47",
                "proofs": [
                    {
                        "key": "10",
                        "value": "0110",
                        "node_hash": "de4ec93c032f5117d8af076dfc86faa5987a6c0b1d52ffc9cf0dfa43989d8c58",
                        "layers": [
                            {
                                "other_hash_side": "left",
                                "other_hash": "1c8ab812b97f5a9da0ba4be2380104810fe5c8022efe9b9e2c9d188fc3537434",
                                "combined_hash": "d340000b3a6717a5a8d42b24516ff69430235c771f8a527554b357b7f03c6de0",
                            },
                            {
                                "other_hash_side": "right",
                                "other_hash": "54e8b4cac761778f396840b343c0f1cb0e1fd0c9927d48d2f0d09a7a6f225126",
                                "combined_hash": "7676004a15439e4e8345d0f9f3a15500805b3447285904b7bcd7d14e27381d2e",
                            },
                            {
                                "other_hash_side": "right",
                                "other_hash": "6a37ca2d9a37a50f2d53387c3cf31395c72d75b1aacfa4402c32dc6d354542b4",
                                "combined_hash": "b1dc97f797a32631483c11d33b4759f5b498b512b7436286d1dc00bb1024b7e2",
                            },
                            {
                                "other_hash_side": "right",
                                "other_hash": "bcff6f16886339a196a2f6c842ad6d350a8579d123eb8602a0a85965ba25d671",
                                "combined_hash": "8e54f5066aa7999fc1561a56df59d11ff01f7df93cadf49a61adebf65dec65ea",
                            },
                        ],
                    }
                ],
            }
        ],
    },
    maker_inclusions=[{"key": b"\x10".hex(), "value": b"\x01\x10".hex()}],
    taker_inclusions=[
        {"key": b"\x10".hex(), "value": b"\x02\x10".hex()},
        {"key": b"\x11".hex(), "value": b"\x02\x11".hex()},
    ],
    trade_id="d00e7c26a45fbbde2160fb5983a9c5e9c17a49a6a9e087a07721fdab913c264d",
    maker_root_history=[
        bytes32.from_hexstr("6661ea6604b491118b0f49c932c0f0de2ad815a57b54b6ec8fdbd1b408ae7e27"),
        bytes32.from_hexstr("8e54f5066aa7999fc1561a56df59d11ff01f7df93cadf49a61adebf65dec65ea"),
    ],
    taker_root_history=[
        bytes32.from_hexstr("42f08ebc0578f2cec7a9ad1c3038e74e0f30eba5c2f4cb1ee1c8fdb682c19dbb"),
        bytes32.from_hexstr("2215da3c9a309e0d8972fd6acb8ac62898a0f7e4a07351d558c2cc5094dfc5ec"),
    ],
)


make_one_existing_take_one_reference = MakeAndTakeReference(
    entries_to_insert=10,
    make_offer_response={
        "trade_id": "deaa16c1e8dcc07568c91238179eda422623c7497e1be858428fdb5b387c9687",
        "offer": "0000000300000000000000000000000000000000000000000000000000000000000000002a5e6ce75ee92c329c576b5dfe550bd8a9054a7b3da5c5f74b1c2c55417fc05f0000000000000000ff02ffff01ff02ffff01ff02ffff03ffff18ff2fff3480ffff01ff04ffff04ff20ffff04ff2fff808080ffff04ffff02ff3effff04ff02ffff04ff05ffff04ffff02ff2affff04ff02ffff04ff27ffff04ffff02ffff03ff77ffff01ff02ff36ffff04ff02ffff04ff09ffff04ff57ffff04ffff02ff2effff04ff02ffff04ff05ff80808080ff808080808080ffff011d80ff0180ffff04ffff02ffff03ff77ffff0181b7ffff015780ff0180ff808080808080ffff04ff77ff808080808080ffff02ff3affff04ff02ffff04ff05ffff04ffff02ff0bff5f80ffff01ff8080808080808080ffff01ff088080ff0180ffff04ffff01ffffffff4947ff0233ffff0401ff0102ffffff20ff02ffff03ff05ffff01ff02ff32ffff04ff02ffff04ff0dffff04ffff0bff3cffff0bff34ff2480ffff0bff3cffff0bff3cffff0bff34ff2c80ff0980ffff0bff3cff0bffff0bff34ff8080808080ff8080808080ffff010b80ff0180ffff02ffff03ffff22ffff09ffff0dff0580ff2280ffff09ffff0dff0b80ff2280ffff15ff17ffff0181ff8080ffff01ff0bff05ff0bff1780ffff01ff088080ff0180ff02ffff03ff0bffff01ff02ffff03ffff02ff26ffff04ff02ffff04ff13ff80808080ffff01ff02ffff03ffff20ff1780ffff01ff02ffff03ffff09ff81b3ffff01818f80ffff01ff02ff3affff04ff02ffff04ff05ffff04ff1bffff04ff34ff808080808080ffff01ff04ffff04ff23ffff04ffff02ff36ffff04ff02ffff04ff09ffff04ff53ffff04ffff02ff2effff04ff02ffff04ff05ff80808080ff808080808080ff738080ffff02ff3affff04ff02ffff04ff05ffff04ff1bffff04ff34ff8080808080808080ff0180ffff01ff088080ff0180ffff01ff04ff13ffff02ff3affff04ff02ffff04ff05ffff04ff1bffff04ff17ff8080808080808080ff0180ffff01ff02ffff03ff17ff80ffff01ff088080ff018080ff0180ffffff02ffff03ffff09ff09ff3880ffff01ff02ffff03ffff18ff2dffff010180ffff01ff0101ff8080ff0180ff8080ff0180ff0bff3cffff0bff34ff2880ffff0bff3cffff0bff3cffff0bff34ff2c80ff0580ffff0bff3cffff02ff32ffff04ff02ffff04ff07ffff04ffff0bff34ff3480ff8080808080ffff0bff34ff8080808080ffff02ffff03ffff07ff0580ffff01ff0bffff0102ffff02ff2effff04ff02ffff04ff09ff80808080ffff02ff2effff04ff02ffff04ff0dff8080808080ffff01ff0bffff0101ff058080ff0180ff02ffff03ffff21ff17ffff09ff0bff158080ffff01ff04ff30ffff04ff0bff808080ffff01ff088080ff0180ff018080ffff04ffff01ffa07faa3253bfddd1e0decb0906b2dc6247bbc4cf608f58345d173adb63e8b47c9fffa07acfcbd1ed73bfe2b698508f4ea5ed353c60ace154360272ce91f9ab0c8423c3a0eff07522495060c066f66f32acc2a77e3a3e737aca8baea4d1a64ea4cdc13da9ffff04ffff01ff02ffff01ff02ffff01ff02ff3effff04ff02ffff04ff05ffff04ffff02ff2fff5f80ffff04ff80ffff04ffff04ffff04ff0bffff04ff17ff808080ffff01ff808080ffff01ff8080808080808080ffff04ffff01ffffff0233ff04ff0101ffff02ff02ffff03ff05ffff01ff02ff1affff04ff02ffff04ff0dffff04ffff0bff12ffff0bff2cff1480ffff0bff12ffff0bff12ffff0bff2cff3c80ff0980ffff0bff12ff0bffff0bff2cff8080808080ff8080808080ffff010b80ff0180ffff0bff12ffff0bff2cff1080ffff0bff12ffff0bff12ffff0bff2cff3c80ff0580ffff0bff12ffff02ff1affff04ff02ffff04ff07ffff04ffff0bff2cff2c80ff8080808080ffff0bff2cff8080808080ffff02ffff03ffff07ff0580ffff01ff0bffff0102ffff02ff2effff04ff02ffff04ff09ff80808080ffff02ff2effff04ff02ffff04ff0dff8080808080ffff01ff0bffff0101ff058080ff0180ff02ffff03ff0bffff01ff02ffff03ffff09ff23ff1880ffff01ff02ffff03ffff18ff81b3ff2c80ffff01ff02ffff03ffff20ff1780ffff01ff02ff3effff04ff02ffff04ff05ffff04ff1bffff04ff33ffff04ff2fffff04ff5fff8080808080808080ffff01ff088080ff0180ffff01ff04ff13ffff02ff3effff04ff02ffff04ff05ffff04ff1bffff04ff17ffff04ff2fffff04ff5fff80808080808080808080ff0180ffff01ff02ffff03ffff09ff23ffff0181e880ffff01ff02ff3effff04ff02ffff04ff05ffff04ff1bffff04ff17ffff04ffff02ffff03ffff22ffff09ffff02ff2effff04ff02ffff04ff53ff80808080ff82014f80ffff20ff5f8080ffff01ff02ff53ffff04ff818fffff04ff82014fffff04ff81b3ff8080808080ffff01ff088080ff0180ffff04ff2cff8080808080808080ffff01ff04ff13ffff02ff3effff04ff02ffff04ff05ffff04ff1bffff04ff17ffff04ff2fffff04ff5fff80808080808080808080ff018080ff0180ffff01ff04ffff04ff18ffff04ffff02ff16ffff04ff02ffff04ff05ffff04ff27ffff04ffff0bff2cff82014f80ffff04ffff02ff2effff04ff02ffff04ff818fff80808080ffff04ffff0bff2cff0580ff8080808080808080ff378080ff81af8080ff0180ff018080ffff04ffff01a0a04d9f57764f54a43e4030befb4d80026e870519aaa66334aef8304f5d0393c2ffff04ffff01ffa042f08ebc0578f2cec7a9ad1c3038e74e0f30eba5c2f4cb1ee1c8fdb682c19dbb80ffff04ffff01a057bfd1cb0adda3d94315053fda723f2028320faa8338225d99f629e3d46d43a9ffff04ffff01ff02ffff01ff02ff0affff04ff02ffff04ff03ff80808080ffff04ffff01ffff333effff02ffff03ff05ffff01ff04ffff04ff0cffff04ffff02ff1effff04ff02ffff04ff09ff80808080ff808080ffff02ff16ffff04ff02ffff04ff19ffff04ffff02ff0affff04ff02ffff04ff0dff80808080ff808080808080ff8080ff0180ffff02ffff03ff05ffff01ff04ffff04ff08ff0980ffff02ff16ffff04ff02ffff04ff0dffff04ff0bff808080808080ffff010b80ff0180ff02ffff03ffff07ff0580ffff01ff0bffff0102ffff02ff1effff04ff02ffff04ff09ff80808080ffff02ff1effff04ff02ffff04ff0dff8080808080ffff01ff0bffff0101ff058080ff0180ff018080ff018080808080ff01808080ffffa00000000000000000000000000000000000000000000000000000000000000000ffffa00000000000000000000000000000000000000000000000000000000000000000ff01ff8080808032dbe6d545f24635c7871ea53c623c358d7cea8f5e27a983ba6e5c0bf35fa243aa064e96a86637d8f5ebe153dc8645d29f43bee762d5ec10d06c8617fa60b8c50000000000000001ff02ffff01ff02ffff01ff02ffff03ffff18ff2fff3480ffff01ff04ffff04ff20ffff04ff2fff808080ffff04ffff02ff3effff04ff02ffff04ff05ffff04ffff02ff2affff04ff02ffff04ff27ffff04ffff02ffff03ff77ffff01ff02ff36ffff04ff02ffff04ff09ffff04ff57ffff04ffff02ff2effff04ff02ffff04ff05ff80808080ff808080808080ffff011d80ff0180ffff04ffff02ffff03ff77ffff0181b7ffff015780ff0180ff808080808080ffff04ff77ff808080808080ffff02ff3affff04ff02ffff04ff05ffff04ffff02ff0bff5f80ffff01ff8080808080808080ffff01ff088080ff0180ffff04ffff01ffffffff4947ff0233ffff0401ff0102ffffff20ff02ffff03ff05ffff01ff02ff32ffff04ff02ffff04ff0dffff04ffff0bff3cffff0bff34ff2480ffff0bff3cffff0bff3cffff0bff34ff2c80ff0980ffff0bff3cff0bffff0bff34ff8080808080ff8080808080ffff010b80ff0180ffff02ffff03ffff22ffff09ffff0dff0580ff2280ffff09ffff0dff0b80ff2280ffff15ff17ffff0181ff8080ffff01ff0bff05ff0bff1780ffff01ff088080ff0180ff02ffff03ff0bffff01ff02ffff03ffff02ff26ffff04ff02ffff04ff13ff80808080ffff01ff02ffff03ffff20ff1780ffff01ff02ffff03ffff09ff81b3ffff01818f80ffff01ff02ff3affff04ff02ffff04ff05ffff04ff1bffff04ff34ff808080808080ffff01ff04ffff04ff23ffff04ffff02ff36ffff04ff02ffff04ff09ffff04ff53ffff04ffff02ff2effff04ff02ffff04ff05ff80808080ff808080808080ff738080ffff02ff3affff04ff02ffff04ff05ffff04ff1bffff04ff34ff8080808080808080ff0180ffff01ff088080ff0180ffff01ff04ff13ffff02ff3affff04ff02ffff04ff05ffff04ff1bffff04ff17ff8080808080808080ff0180ffff01ff02ffff03ff17ff80ffff01ff088080ff018080ff0180ffffff02ffff03ffff09ff09ff3880ffff01ff02ffff03ffff18ff2dffff010180ffff01ff0101ff8080ff0180ff8080ff0180ff0bff3cffff0bff34ff2880ffff0bff3cffff0bff3cffff0bff34ff2c80ff0580ffff0bff3cffff02ff32ffff04ff02ffff04ff07ffff04ffff0bff34ff3480ff8080808080ffff0bff34ff8080808080ffff02ffff03ffff07ff0580ffff01ff0bffff0102ffff02ff2effff04ff02ffff04ff09ff80808080ffff02ff2effff04ff02ffff04ff0dff8080808080ffff01ff0bffff0101ff058080ff0180ff02ffff03ffff21ff17ffff09ff0bff158080ffff01ff04ff30ffff04ff0bff808080ffff01ff088080ff0180ff018080ffff04ffff01ffa07faa3253bfddd1e0decb0906b2dc6247bbc4cf608f58345d173adb63e8b47c9fffa0a14daf55d41ced6419bcd011fbc1f74ab9567fe55340d88435aa6493d628fa47a0eff07522495060c066f66f32acc2a77e3a3e737aca8baea4d1a64ea4cdc13da9ffff04ffff01ff02ffff01ff02ffff01ff02ff3effff04ff02ffff04ff05ffff04ffff02ff2fff5f80ffff04ff80ffff04ffff04ffff04ff0bffff04ff17ff808080ffff01ff808080ffff01ff8080808080808080ffff04ffff01ffffff0233ff04ff0101ffff02ff02ffff03ff05ffff01ff02ff1affff04ff02ffff04ff0dffff04ffff0bff12ffff0bff2cff1480ffff0bff12ffff0bff12ffff0bff2cff3c80ff0980ffff0bff12ff0bffff0bff2cff8080808080ff8080808080ffff010b80ff0180ffff0bff12ffff0bff2cff1080ffff0bff12ffff0bff12ffff0bff2cff3c80ff0580ffff0bff12ffff02ff1affff04ff02ffff04ff07ffff04ffff0bff2cff2c80ff8080808080ffff0bff2cff8080808080ffff02ffff03ffff07ff0580ffff01ff0bffff0102ffff02ff2effff04ff02ffff04ff09ff80808080ffff02ff2effff04ff02ffff04ff0dff8080808080ffff01ff0bffff0101ff058080ff0180ff02ffff03ff0bffff01ff02ffff03ffff09ff23ff1880ffff01ff02ffff03ffff18ff81b3ff2c80ffff01ff02ffff03ffff20ff1780ffff01ff02ff3effff04ff02ffff04ff05ffff04ff1bffff04ff33ffff04ff2fffff04ff5fff8080808080808080ffff01ff088080ff0180ffff01ff04ff13ffff02ff3effff04ff02ffff04ff05ffff04ff1bffff04ff17ffff04ff2fffff04ff5fff80808080808080808080ff0180ffff01ff02ffff03ffff09ff23ffff0181e880ffff01ff02ff3effff04ff02ffff04ff05ffff04ff1bffff04ff17ffff04ffff02ffff03ffff22ffff09ffff02ff2effff04ff02ffff04ff53ff80808080ff82014f80ffff20ff5f8080ffff01ff02ff53ffff04ff818fffff04ff82014fffff04ff81b3ff8080808080ffff01ff088080ff0180ffff04ff2cff8080808080808080ffff01ff04ff13ffff02ff3effff04ff02ffff04ff05ffff04ff1bffff04ff17ffff04ff2fffff04ff5fff80808080808080808080ff018080ff0180ffff01ff04ffff04ff18ffff04ffff02ff16ffff04ff02ffff04ff05ffff04ff27ffff04ffff0bff2cff82014f80ffff04ffff02ff2effff04ff02ffff04ff818fff80808080ffff04ffff0bff2cff0580ff8080808080808080ff378080ff81af8080ff0180ff018080ffff04ffff01a0a04d9f57764f54a43e4030befb4d80026e870519aaa66334aef8304f5d0393c2ffff04ffff01ffa06661ea6604b491118b0f49c932c0f0de2ad815a57b54b6ec8fdbd1b408ae7e2780ffff04ffff01a057bfd1cb0adda3d94315053fda723f2028320faa8338225d99f629e3d46d43a9ffff04ffff01ff02ffff01ff02ffff01ff02ffff03ff0bffff01ff02ffff03ffff09ff05ffff1dff0bffff1effff0bff0bffff02ff06ffff04ff02ffff04ff17ff8080808080808080ffff01ff02ff17ff2f80ffff01ff088080ff0180ffff01ff04ffff04ff04ffff04ff05ffff04ffff02ff06ffff04ff02ffff04ff17ff80808080ff80808080ffff02ff17ff2f808080ff0180ffff04ffff01ff32ff02ffff03ffff07ff0580ffff01ff0bffff0102ffff02ff06ffff04ff02ffff04ff09ff80808080ffff02ff06ffff04ff02ffff04ff0dff8080808080ffff01ff0bffff0101ff058080ff0180ff018080ffff04ffff01b0a132fae32c98cbb7d8f5814c49ee3f0ba6ec2172c5e5f6900655a65cd2157a06a1c6eb89c68c8d2cdcee9506c2217978ff018080ff018080808080ff01808080ffffa0a14daf55d41ced6419bcd011fbc1f74ab9567fe55340d88435aa6493d628fa47ffa01804338c97f989c78d88716206c0f27315f3eb7d59417ab2eacee20f0a7ff60bff0180ff01ffffff80ffff02ffff01ff02ffff01ff02ffff03ff5fffff01ff02ff3affff04ff02ffff04ff0bffff04ff17ffff04ff2fffff04ff5fffff04ff81bfffff04ff82017fffff04ff8202ffffff04ffff02ff05ff8205ff80ff8080808080808080808080ffff01ff04ffff04ff10ffff01ff81ff8080ffff02ff05ff8205ff808080ff0180ffff04ffff01ffffff49ff3f02ff04ff0101ffff02ffff02ffff03ff05ffff01ff02ff2affff04ff02ffff04ff0dffff04ffff0bff12ffff0bff2cff1480ffff0bff12ffff0bff12ffff0bff2cff3c80ff0980ffff0bff12ff0bffff0bff2cff8080808080ff8080808080ffff010b80ff0180ff02ffff03ff05ffff01ff02ffff03ffff02ff3effff04ff02ffff04ff82011fffff04ff27ffff04ff4fff808080808080ffff01ff02ff3affff04ff02ffff04ff0dffff04ff1bffff04ff37ffff04ff6fffff04ff81dfffff04ff8201bfffff04ff82037fffff04ffff04ffff04ff28ffff04ffff0bffff02ff26ffff04ff02ffff04ff11ffff04ffff02ff26ffff04ff02ffff04ff13ffff04ff82027fffff04ffff02ff36ffff04ff02ffff04ff82013fff80808080ffff04ffff02ff36ffff04ff02ffff04ff819fff80808080ffff04ffff02ff36ffff04ff02ffff04ff13ff80808080ff8080808080808080ffff04ffff02ff36ffff04ff02ffff04ff09ff80808080ff808080808080ffff012480ff808080ff8202ff80ff8080808080808080808080ffff01ff088080ff0180ffff018202ff80ff0180ffffff0bff12ffff0bff2cff3880ffff0bff12ffff0bff12ffff0bff2cff3c80ff0580ffff0bff12ffff02ff2affff04ff02ffff04ff07ffff04ffff0bff2cff2c80ff8080808080ffff0bff2cff8080808080ff02ffff03ffff07ff0580ffff01ff0bffff0102ffff02ff36ffff04ff02ffff04ff09ff80808080ffff02ff36ffff04ff02ffff04ff0dff8080808080ffff01ff0bffff0101ff058080ff0180ffff02ffff03ff1bffff01ff02ff2effff04ff02ffff04ffff02ffff03ffff18ffff0101ff1380ffff01ff0bffff0102ff2bff0580ffff01ff0bffff0102ff05ff2b8080ff0180ffff04ffff04ffff17ff13ffff0181ff80ff3b80ff8080808080ffff010580ff0180ff02ffff03ff17ffff01ff02ffff03ffff09ff05ffff02ff2effff04ff02ffff04ff13ffff04ff27ff808080808080ffff01ff02ff3effff04ff02ffff04ff05ffff04ff1bffff04ff37ff808080808080ffff01ff088080ff0180ffff01ff010180ff0180ff018080ffff04ffff01ff01ffff33ffa09b077471a29fd048bf897998e3f73ee5215345cd4943441e7c654dc11f2c579eff01ffffa0a14daf55d41ced6419bcd011fbc1f74ab9567fe55340d88435aa6493d628fa47ffa06661ea6604b491118b0f49c932c0f0de2ad815a57b54b6ec8fdbd1b408ae7e27ffa0c842b1a384b8633ac25d0f12bd7b614f86a77642ab6426418750f2b0b86bab2a8080ffff3fffa06766ecb6a87bcab8829fc9b3f08c8b5a83ecb7c5597c6a13ec346bfcafc1aab88080ffff04ffff01ffffa07faa3253bfddd1e0decb0906b2dc6247bbc4cf608f58345d173adb63e8b47c9fffa07acfcbd1ed73bfe2b698508f4ea5ed353c60ace154360272ce91f9ab0c8423c3a0eff07522495060c066f66f32acc2a77e3a3e737aca8baea4d1a64ea4cdc13da980ffff04ffff01ffa0a04d9f57764f54a43e4030befb4d80026e870519aaa66334aef8304f5d0393c280ffff04ffff01ffffa07f3e180acdf046f955d3440bb3a16dfd6f5a46c809cee98e7514127327b1cab58080ff018080808080ffff80ff80ff80ff80ff8080808080ca2e21c90d263e63b73d449a3f8d57b9458846f7af27d9a61a515395fa14071ef1932b0458af07a67925e9e0d5eca3ae137ba72bc689bd9b7b00bd0508ee6be80000000000000001ff02ffff01ff02ffff01ff02ffff03ffff18ff2fff3480ffff01ff04ffff04ff20ffff04ff2fff808080ffff04ffff02ff3effff04ff02ffff04ff05ffff04ffff02ff2affff04ff02ffff04ff27ffff04ffff02ffff03ff77ffff01ff02ff36ffff04ff02ffff04ff09ffff04ff57ffff04ffff02ff2effff04ff02ffff04ff05ff80808080ff808080808080ffff011d80ff0180ffff04ffff02ffff03ff77ffff0181b7ffff015780ff0180ff808080808080ffff04ff77ff808080808080ffff02ff3affff04ff02ffff04ff05ffff04ffff02ff0bff5f80ffff01ff8080808080808080ffff01ff088080ff0180ffff04ffff01ffffffff4947ff0233ffff0401ff0102ffffff20ff02ffff03ff05ffff01ff02ff32ffff04ff02ffff04ff0dffff04ffff0bff3cffff0bff34ff2480ffff0bff3cffff0bff3cffff0bff34ff2c80ff0980ffff0bff3cff0bffff0bff34ff8080808080ff8080808080ffff010b80ff0180ffff02ffff03ffff22ffff09ffff0dff0580ff2280ffff09ffff0dff0b80ff2280ffff15ff17ffff0181ff8080ffff01ff0bff05ff0bff1780ffff01ff088080ff0180ff02ffff03ff0bffff01ff02ffff03ffff02ff26ffff04ff02ffff04ff13ff80808080ffff01ff02ffff03ffff20ff1780ffff01ff02ffff03ffff09ff81b3ffff01818f80ffff01ff02ff3affff04ff02ffff04ff05ffff04ff1bffff04ff34ff808080808080ffff01ff04ffff04ff23ffff04ffff02ff36ffff04ff02ffff04ff09ffff04ff53ffff04ffff02ff2effff04ff02ffff04ff05ff80808080ff808080808080ff738080ffff02ff3affff04ff02ffff04ff05ffff04ff1bffff04ff34ff8080808080808080ff0180ffff01ff088080ff0180ffff01ff04ff13ffff02ff3affff04ff02ffff04ff05ffff04ff1bffff04ff17ff8080808080808080ff0180ffff01ff02ffff03ff17ff80ffff01ff088080ff018080ff0180ffffff02ffff03ffff09ff09ff3880ffff01ff02ffff03ffff18ff2dffff010180ffff01ff0101ff8080ff0180ff8080ff0180ff0bff3cffff0bff34ff2880ffff0bff3cffff0bff3cffff0bff34ff2c80ff0580ffff0bff3cffff02ff32ffff04ff02ffff04ff07ffff04ffff0bff34ff3480ff8080808080ffff0bff34ff8080808080ffff02ffff03ffff07ff0580ffff01ff0bffff0102ffff02ff2effff04ff02ffff04ff09ff80808080ffff02ff2effff04ff02ffff04ff0dff8080808080ffff01ff0bffff0101ff058080ff0180ff02ffff03ffff21ff17ffff09ff0bff158080ffff01ff04ff30ffff04ff0bff808080ffff01ff088080ff0180ff018080ffff04ffff01ffa07faa3253bfddd1e0decb0906b2dc6247bbc4cf608f58345d173adb63e8b47c9fffa0a14daf55d41ced6419bcd011fbc1f74ab9567fe55340d88435aa6493d628fa47a0eff07522495060c066f66f32acc2a77e3a3e737aca8baea4d1a64ea4cdc13da9ffff04ffff01ff02ffff01ff02ffff01ff02ff3effff04ff02ffff04ff05ffff04ffff02ff2fff5f80ffff04ff80ffff04ffff04ffff04ff0bffff04ff17ff808080ffff01ff808080ffff01ff8080808080808080ffff04ffff01ffffff0233ff04ff0101ffff02ff02ffff03ff05ffff01ff02ff1affff04ff02ffff04ff0dffff04ffff0bff12ffff0bff2cff1480ffff0bff12ffff0bff12ffff0bff2cff3c80ff0980ffff0bff12ff0bffff0bff2cff8080808080ff8080808080ffff010b80ff0180ffff0bff12ffff0bff2cff1080ffff0bff12ffff0bff12ffff0bff2cff3c80ff0580ffff0bff12ffff02ff1affff04ff02ffff04ff07ffff04ffff0bff2cff2c80ff8080808080ffff0bff2cff8080808080ffff02ffff03ffff07ff0580ffff01ff0bffff0102ffff02ff2effff04ff02ffff04ff09ff80808080ffff02ff2effff04ff02ffff04ff0dff8080808080ffff01ff0bffff0101ff058080ff0180ff02ffff03ff0bffff01ff02ffff03ffff09ff23ff1880ffff01ff02ffff03ffff18ff81b3ff2c80ffff01ff02ffff03ffff20ff1780ffff01ff02ff3effff04ff02ffff04ff05ffff04ff1bffff04ff33ffff04ff2fffff04ff5fff8080808080808080ffff01ff088080ff0180ffff01ff04ff13ffff02ff3effff04ff02ffff04ff05ffff04ff1bffff04ff17ffff04ff2fffff04ff5fff80808080808080808080ff0180ffff01ff02ffff03ffff09ff23ffff0181e880ffff01ff02ff3effff04ff02ffff04ff05ffff04ff1bffff04ff17ffff04ffff02ffff03ffff22ffff09ffff02ff2effff04ff02ffff04ff53ff80808080ff82014f80ffff20ff5f8080ffff01ff02ff53ffff04ff818fffff04ff82014fffff04ff81b3ff8080808080ffff01ff088080ff0180ffff04ff2cff8080808080808080ffff01ff04ff13ffff02ff3effff04ff02ffff04ff05ffff04ff1bffff04ff17ffff04ff2fffff04ff5fff80808080808080808080ff018080ff0180ffff01ff04ffff04ff18ffff04ffff02ff16ffff04ff02ffff04ff05ffff04ff27ffff04ffff0bff2cff82014f80ffff04ffff02ff2effff04ff02ffff04ff818fff80808080ffff04ffff0bff2cff0580ff8080808080808080ff378080ff81af8080ff0180ff018080ffff04ffff01a0a04d9f57764f54a43e4030befb4d80026e870519aaa66334aef8304f5d0393c2ffff04ffff01ffa06661ea6604b491118b0f49c932c0f0de2ad815a57b54b6ec8fdbd1b408ae7e2780ffff04ffff01a057bfd1cb0adda3d94315053fda723f2028320faa8338225d99f629e3d46d43a9ffff04ffff01ff01ffff33ffa0c842b1a384b8633ac25d0f12bd7b614f86a77642ab6426418750f2b0b86bab2aff01ffffa0a14daf55d41ced6419bcd011fbc1f74ab9567fe55340d88435aa6493d628fa47ffa06661ea6604b491118b0f49c932c0f0de2ad815a57b54b6ec8fdbd1b408ae7e27ffa0c842b1a384b8633ac25d0f12bd7b614f86a77642ab6426418750f2b0b86bab2a8080ffff3eff248080ff018080808080ff01808080ffffa032dbe6d545f24635c7871ea53c623c358d7cea8f5e27a983ba6e5c0bf35fa243ffa08c4aebb18e8ce08405083c3d90a29f30239865142e2dcbca5393f40df9e3821dff0180ff01ffff808080a2028ecf85b4d00dc64b8da4afc4c636a80df8068e77166e8b40b57cbafba5de414570001536d1c431dc281191d8ab3e082e4614135872b18887e230853d0ee92b6f7f45910f2ad0bfecb34519efc8f1bb9fa24ee426a1ee18522f5779236cfa",  # noqa: E501
        "taker": [
            {
                "store_id": "7acfcbd1ed73bfe2b698508f4ea5ed353c60ace154360272ce91f9ab0c8423c3",
                "inclusions": [{"key": "10", "value": "0210"}],
            }
        ],
        "maker": [
            {
                "store_id": "a14daf55d41ced6419bcd011fbc1f74ab9567fe55340d88435aa6493d628fa47",
                "proofs": [
                    {
                        "key": "09",
                        "value": "0109",
                        "node_hash": "0ab4218d9b9763bb4978723b6bc3dcde8a952c49d6d6bbdebf9753e33ae94a4d",
                        "layers": [
                            {
                                "other_hash_side": "right",
                                "other_hash": "a9f71348ec8cf151e38fe0f6aa841dff3eac1f5a34161147e700ca179d2f7189",
                                "combined_hash": "577ab817898afbd1e149b8933322226fcd1e38f4c3921e7700dddae8c886996d",
                            },
                            {
                                "other_hash_side": "left",
                                "other_hash": "0e81e890b50e4547357938fb9cb81c7f17178e3f1bc47b784cd139ef9707c045",
                                "combined_hash": "f6a103cb21324e62dd5ca99eac6649b33be36af0b22588801c51196b571713ff",
                            },
                            {
                                "other_hash_side": "left",
                                "other_hash": "b2586f6b7a4a76e99064549f324d53c7f60eb2d5f67bd0c24444d0167dc7dc01",
                                "combined_hash": "2a6b820ed0e775d7f2e88fbbac8908f09880ca75a6f7e0f8611c940ee2dc8cec",
                            },
                            {
                                "other_hash_side": "right",
                                "other_hash": "ff63e1ccbbd40190042ed1ed2b553e264828065d1bb7fd3fe479d4444223e043",
                                "combined_hash": "bcff6f16886339a196a2f6c842ad6d350a8579d123eb8602a0a85965ba25d671",
                            },
                            {
                                "other_hash_side": "left",
                                "other_hash": "980a121e80381e79b37aa634758ff8a56c6cdf67c50ec0e75d14b4749dcde189",
                                "combined_hash": "6661ea6604b491118b0f49c932c0f0de2ad815a57b54b6ec8fdbd1b408ae7e27",
                            },
                        ],
                    }
                ],
            }
        ],
    },
    maker_inclusions=[{"key": b"\x09".hex(), "value": b"\x01\x09".hex()}],
    taker_inclusions=[{"key": b"\x10".hex(), "value": b"\x02\x10".hex()}],
    trade_id="61897a9f70200192924207b99706480271d17ea0107ea8a2b250f4b6624ed110",
    maker_root_history=[
        bytes32.from_hexstr("6661ea6604b491118b0f49c932c0f0de2ad815a57b54b6ec8fdbd1b408ae7e27"),
    ],
    taker_root_history=[
        bytes32.from_hexstr("42f08ebc0578f2cec7a9ad1c3038e74e0f30eba5c2f4cb1ee1c8fdb682c19dbb"),
        bytes32.from_hexstr("eeb63ac765065d2ee161e1c059c8188ef809e1c3ed8739bad5bfee2c2ee1c742"),
    ],
)


make_one_take_one_existing_reference = MakeAndTakeReference(
    entries_to_insert=10,
    make_offer_response={
        "trade_id": "beafa879abbffc8236b098758c1af73e6fe4e31c49100f35f2cf3a5ade1cefc4",
        "offer": "0000000300000000000000000000000000000000000000000000000000000000000000002a5e6ce75ee92c329c576b5dfe550bd8a9054a7b3da5c5f74b1c2c55417fc05f0000000000000000ff02ffff01ff02ffff01ff02ffff03ffff18ff2fff3480ffff01ff04ffff04ff20ffff04ff2fff808080ffff04ffff02ff3effff04ff02ffff04ff05ffff04ffff02ff2affff04ff02ffff04ff27ffff04ffff02ffff03ff77ffff01ff02ff36ffff04ff02ffff04ff09ffff04ff57ffff04ffff02ff2effff04ff02ffff04ff05ff80808080ff808080808080ffff011d80ff0180ffff04ffff02ffff03ff77ffff0181b7ffff015780ff0180ff808080808080ffff04ff77ff808080808080ffff02ff3affff04ff02ffff04ff05ffff04ffff02ff0bff5f80ffff01ff8080808080808080ffff01ff088080ff0180ffff04ffff01ffffffff4947ff0233ffff0401ff0102ffffff20ff02ffff03ff05ffff01ff02ff32ffff04ff02ffff04ff0dffff04ffff0bff3cffff0bff34ff2480ffff0bff3cffff0bff3cffff0bff34ff2c80ff0980ffff0bff3cff0bffff0bff34ff8080808080ff8080808080ffff010b80ff0180ffff02ffff03ffff22ffff09ffff0dff0580ff2280ffff09ffff0dff0b80ff2280ffff15ff17ffff0181ff8080ffff01ff0bff05ff0bff1780ffff01ff088080ff0180ff02ffff03ff0bffff01ff02ffff03ffff02ff26ffff04ff02ffff04ff13ff80808080ffff01ff02ffff03ffff20ff1780ffff01ff02ffff03ffff09ff81b3ffff01818f80ffff01ff02ff3affff04ff02ffff04ff05ffff04ff1bffff04ff34ff808080808080ffff01ff04ffff04ff23ffff04ffff02ff36ffff04ff02ffff04ff09ffff04ff53ffff04ffff02ff2effff04ff02ffff04ff05ff80808080ff808080808080ff738080ffff02ff3affff04ff02ffff04ff05ffff04ff1bffff04ff34ff8080808080808080ff0180ffff01ff088080ff0180ffff01ff04ff13ffff02ff3affff04ff02ffff04ff05ffff04ff1bffff04ff17ff8080808080808080ff0180ffff01ff02ffff03ff17ff80ffff01ff088080ff018080ff0180ffffff02ffff03ffff09ff09ff3880ffff01ff02ffff03ffff18ff2dffff010180ffff01ff0101ff8080ff0180ff8080ff0180ff0bff3cffff0bff34ff2880ffff0bff3cffff0bff3cffff0bff34ff2c80ff0580ffff0bff3cffff02ff32ffff04ff02ffff04ff07ffff04ffff0bff34ff3480ff8080808080ffff0bff34ff8080808080ffff02ffff03ffff07ff0580ffff01ff0bffff0102ffff02ff2effff04ff02ffff04ff09ff80808080ffff02ff2effff04ff02ffff04ff0dff8080808080ffff01ff0bffff0101ff058080ff0180ff02ffff03ffff21ff17ffff09ff0bff158080ffff01ff04ff30ffff04ff0bff808080ffff01ff088080ff0180ff018080ffff04ffff01ffa07faa3253bfddd1e0decb0906b2dc6247bbc4cf608f58345d173adb63e8b47c9fffa07acfcbd1ed73bfe2b698508f4ea5ed353c60ace154360272ce91f9ab0c8423c3a0eff07522495060c066f66f32acc2a77e3a3e737aca8baea4d1a64ea4cdc13da9ffff04ffff01ff02ffff01ff02ffff01ff02ff3effff04ff02ffff04ff05ffff04ffff02ff2fff5f80ffff04ff80ffff04ffff04ffff04ff0bffff04ff17ff808080ffff01ff808080ffff01ff8080808080808080ffff04ffff01ffffff0233ff04ff0101ffff02ff02ffff03ff05ffff01ff02ff1affff04ff02ffff04ff0dffff04ffff0bff12ffff0bff2cff1480ffff0bff12ffff0bff12ffff0bff2cff3c80ff0980ffff0bff12ff0bffff0bff2cff8080808080ff8080808080ffff010b80ff0180ffff0bff12ffff0bff2cff1080ffff0bff12ffff0bff12ffff0bff2cff3c80ff0580ffff0bff12ffff02ff1affff04ff02ffff04ff07ffff04ffff0bff2cff2c80ff8080808080ffff0bff2cff8080808080ffff02ffff03ffff07ff0580ffff01ff0bffff0102ffff02ff2effff04ff02ffff04ff09ff80808080ffff02ff2effff04ff02ffff04ff0dff8080808080ffff01ff0bffff0101ff058080ff0180ff02ffff03ff0bffff01ff02ffff03ffff09ff23ff1880ffff01ff02ffff03ffff18ff81b3ff2c80ffff01ff02ffff03ffff20ff1780ffff01ff02ff3effff04ff02ffff04ff05ffff04ff1bffff04ff33ffff04ff2fffff04ff5fff8080808080808080ffff01ff088080ff0180ffff01ff04ff13ffff02ff3effff04ff02ffff04ff05ffff04ff1bffff04ff17ffff04ff2fffff04ff5fff80808080808080808080ff0180ffff01ff02ffff03ffff09ff23ffff0181e880ffff01ff02ff3effff04ff02ffff04ff05ffff04ff1bffff04ff17ffff04ffff02ffff03ffff22ffff09ffff02ff2effff04ff02ffff04ff53ff80808080ff82014f80ffff20ff5f8080ffff01ff02ff53ffff04ff818fffff04ff82014fffff04ff81b3ff8080808080ffff01ff088080ff0180ffff04ff2cff8080808080808080ffff01ff04ff13ffff02ff3effff04ff02ffff04ff05ffff04ff1bffff04ff17ffff04ff2fffff04ff5fff80808080808080808080ff018080ff0180ffff01ff04ffff04ff18ffff04ffff02ff16ffff04ff02ffff04ff05ffff04ff27ffff04ffff0bff2cff82014f80ffff04ffff02ff2effff04ff02ffff04ff818fff80808080ffff04ffff0bff2cff0580ff8080808080808080ff378080ff81af8080ff0180ff018080ffff04ffff01a0a04d9f57764f54a43e4030befb4d80026e870519aaa66334aef8304f5d0393c2ffff04ffff01ffa042f08ebc0578f2cec7a9ad1c3038e74e0f30eba5c2f4cb1ee1c8fdb682c19dbb80ffff04ffff01a057bfd1cb0adda3d94315053fda723f2028320faa8338225d99f629e3d46d43a9ffff04ffff01ff02ffff01ff02ff0affff04ff02ffff04ff03ff80808080ffff04ffff01ffff333effff02ffff03ff05ffff01ff04ffff04ff0cffff04ffff02ff1effff04ff02ffff04ff09ff80808080ff808080ffff02ff16ffff04ff02ffff04ff19ffff04ffff02ff0affff04ff02ffff04ff0dff80808080ff808080808080ff8080ff0180ffff02ffff03ff05ffff01ff04ffff04ff08ff0980ffff02ff16ffff04ff02ffff04ff0dffff04ff0bff808080808080ffff010b80ff0180ff02ffff03ffff07ff0580ffff01ff0bffff0102ffff02ff1effff04ff02ffff04ff09ff80808080ffff02ff1effff04ff02ffff04ff0dff8080808080ffff01ff0bffff0101ff058080ff0180ff018080ff018080808080ff01808080ffffa00000000000000000000000000000000000000000000000000000000000000000ffffa00000000000000000000000000000000000000000000000000000000000000000ff01ff8080808032dbe6d545f24635c7871ea53c623c358d7cea8f5e27a983ba6e5c0bf35fa243aa064e96a86637d8f5ebe153dc8645d29f43bee762d5ec10d06c8617fa60b8c50000000000000001ff02ffff01ff02ffff01ff02ffff03ffff18ff2fff3480ffff01ff04ffff04ff20ffff04ff2fff808080ffff04ffff02ff3effff04ff02ffff04ff05ffff04ffff02ff2affff04ff02ffff04ff27ffff04ffff02ffff03ff77ffff01ff02ff36ffff04ff02ffff04ff09ffff04ff57ffff04ffff02ff2effff04ff02ffff04ff05ff80808080ff808080808080ffff011d80ff0180ffff04ffff02ffff03ff77ffff0181b7ffff015780ff0180ff808080808080ffff04ff77ff808080808080ffff02ff3affff04ff02ffff04ff05ffff04ffff02ff0bff5f80ffff01ff8080808080808080ffff01ff088080ff0180ffff04ffff01ffffffff4947ff0233ffff0401ff0102ffffff20ff02ffff03ff05ffff01ff02ff32ffff04ff02ffff04ff0dffff04ffff0bff3cffff0bff34ff2480ffff0bff3cffff0bff3cffff0bff34ff2c80ff0980ffff0bff3cff0bffff0bff34ff8080808080ff8080808080ffff010b80ff0180ffff02ffff03ffff22ffff09ffff0dff0580ff2280ffff09ffff0dff0b80ff2280ffff15ff17ffff0181ff8080ffff01ff0bff05ff0bff1780ffff01ff088080ff0180ff02ffff03ff0bffff01ff02ffff03ffff02ff26ffff04ff02ffff04ff13ff80808080ffff01ff02ffff03ffff20ff1780ffff01ff02ffff03ffff09ff81b3ffff01818f80ffff01ff02ff3affff04ff02ffff04ff05ffff04ff1bffff04ff34ff808080808080ffff01ff04ffff04ff23ffff04ffff02ff36ffff04ff02ffff04ff09ffff04ff53ffff04ffff02ff2effff04ff02ffff04ff05ff80808080ff808080808080ff738080ffff02ff3affff04ff02ffff04ff05ffff04ff1bffff04ff34ff8080808080808080ff0180ffff01ff088080ff0180ffff01ff04ff13ffff02ff3affff04ff02ffff04ff05ffff04ff1bffff04ff17ff8080808080808080ff0180ffff01ff02ffff03ff17ff80ffff01ff088080ff018080ff0180ffffff02ffff03ffff09ff09ff3880ffff01ff02ffff03ffff18ff2dffff010180ffff01ff0101ff8080ff0180ff8080ff0180ff0bff3cffff0bff34ff2880ffff0bff3cffff0bff3cffff0bff34ff2c80ff0580ffff0bff3cffff02ff32ffff04ff02ffff04ff07ffff04ffff0bff34ff3480ff8080808080ffff0bff34ff8080808080ffff02ffff03ffff07ff0580ffff01ff0bffff0102ffff02ff2effff04ff02ffff04ff09ff80808080ffff02ff2effff04ff02ffff04ff0dff8080808080ffff01ff0bffff0101ff058080ff0180ff02ffff03ffff21ff17ffff09ff0bff158080ffff01ff04ff30ffff04ff0bff808080ffff01ff088080ff0180ff018080ffff04ffff01ffa07faa3253bfddd1e0decb0906b2dc6247bbc4cf608f58345d173adb63e8b47c9fffa0a14daf55d41ced6419bcd011fbc1f74ab9567fe55340d88435aa6493d628fa47a0eff07522495060c066f66f32acc2a77e3a3e737aca8baea4d1a64ea4cdc13da9ffff04ffff01ff02ffff01ff02ffff01ff02ff3effff04ff02ffff04ff05ffff04ffff02ff2fff5f80ffff04ff80ffff04ffff04ffff04ff0bffff04ff17ff808080ffff01ff808080ffff01ff8080808080808080ffff04ffff01ffffff0233ff04ff0101ffff02ff02ffff03ff05ffff01ff02ff1affff04ff02ffff04ff0dffff04ffff0bff12ffff0bff2cff1480ffff0bff12ffff0bff12ffff0bff2cff3c80ff0980ffff0bff12ff0bffff0bff2cff8080808080ff8080808080ffff010b80ff0180ffff0bff12ffff0bff2cff1080ffff0bff12ffff0bff12ffff0bff2cff3c80ff0580ffff0bff12ffff02ff1affff04ff02ffff04ff07ffff04ffff0bff2cff2c80ff8080808080ffff0bff2cff8080808080ffff02ffff03ffff07ff0580ffff01ff0bffff0102ffff02ff2effff04ff02ffff04ff09ff80808080ffff02ff2effff04ff02ffff04ff0dff8080808080ffff01ff0bffff0101ff058080ff0180ff02ffff03ff0bffff01ff02ffff03ffff09ff23ff1880ffff01ff02ffff03ffff18ff81b3ff2c80ffff01ff02ffff03ffff20ff1780ffff01ff02ff3effff04ff02ffff04ff05ffff04ff1bffff04ff33ffff04ff2fffff04ff5fff8080808080808080ffff01ff088080ff0180ffff01ff04ff13ffff02ff3effff04ff02ffff04ff05ffff04ff1bffff04ff17ffff04ff2fffff04ff5fff80808080808080808080ff0180ffff01ff02ffff03ffff09ff23ffff0181e880ffff01ff02ff3effff04ff02ffff04ff05ffff04ff1bffff04ff17ffff04ffff02ffff03ffff22ffff09ffff02ff2effff04ff02ffff04ff53ff80808080ff82014f80ffff20ff5f8080ffff01ff02ff53ffff04ff818fffff04ff82014fffff04ff81b3ff8080808080ffff01ff088080ff0180ffff04ff2cff8080808080808080ffff01ff04ff13ffff02ff3effff04ff02ffff04ff05ffff04ff1bffff04ff17ffff04ff2fffff04ff5fff80808080808080808080ff018080ff0180ffff01ff04ffff04ff18ffff04ffff02ff16ffff04ff02ffff04ff05ffff04ff27ffff04ffff0bff2cff82014f80ffff04ffff02ff2effff04ff02ffff04ff818fff80808080ffff04ffff0bff2cff0580ff8080808080808080ff378080ff81af8080ff0180ff018080ffff04ffff01a0a04d9f57764f54a43e4030befb4d80026e870519aaa66334aef8304f5d0393c2ffff04ffff01ffa06661ea6604b491118b0f49c932c0f0de2ad815a57b54b6ec8fdbd1b408ae7e2780ffff04ffff01a057bfd1cb0adda3d94315053fda723f2028320faa8338225d99f629e3d46d43a9ffff04ffff01ff02ffff01ff02ffff01ff02ffff03ff0bffff01ff02ffff03ffff09ff05ffff1dff0bffff1effff0bff0bffff02ff06ffff04ff02ffff04ff17ff8080808080808080ffff01ff02ff17ff2f80ffff01ff088080ff0180ffff01ff04ffff04ff04ffff04ff05ffff04ffff02ff06ffff04ff02ffff04ff17ff80808080ff80808080ffff02ff17ff2f808080ff0180ffff04ffff01ff32ff02ffff03ffff07ff0580ffff01ff0bffff0102ffff02ff06ffff04ff02ffff04ff09ff80808080ffff02ff06ffff04ff02ffff04ff0dff8080808080ffff01ff0bffff0101ff058080ff0180ff018080ffff04ffff01b0a132fae32c98cbb7d8f5814c49ee3f0ba6ec2172c5e5f6900655a65cd2157a06a1c6eb89c68c8d2cdcee9506c2217978ff018080ff018080808080ff01808080ffffa0a14daf55d41ced6419bcd011fbc1f74ab9567fe55340d88435aa6493d628fa47ffa01804338c97f989c78d88716206c0f27315f3eb7d59417ab2eacee20f0a7ff60bff0180ff01ffffff80ffff02ffff01ff02ffff01ff02ffff03ff5fffff01ff02ff3affff04ff02ffff04ff0bffff04ff17ffff04ff2fffff04ff5fffff04ff81bfffff04ff82017fffff04ff8202ffffff04ffff02ff05ff8205ff80ff8080808080808080808080ffff01ff04ffff04ff10ffff01ff81ff8080ffff02ff05ff8205ff808080ff0180ffff04ffff01ffffff49ff3f02ff04ff0101ffff02ffff02ffff03ff05ffff01ff02ff2affff04ff02ffff04ff0dffff04ffff0bff12ffff0bff2cff1480ffff0bff12ffff0bff12ffff0bff2cff3c80ff0980ffff0bff12ff0bffff0bff2cff8080808080ff8080808080ffff010b80ff0180ff02ffff03ff05ffff01ff02ffff03ffff02ff3effff04ff02ffff04ff82011fffff04ff27ffff04ff4fff808080808080ffff01ff02ff3affff04ff02ffff04ff0dffff04ff1bffff04ff37ffff04ff6fffff04ff81dfffff04ff8201bfffff04ff82037fffff04ffff04ffff04ff28ffff04ffff0bffff02ff26ffff04ff02ffff04ff11ffff04ffff02ff26ffff04ff02ffff04ff13ffff04ff82027fffff04ffff02ff36ffff04ff02ffff04ff82013fff80808080ffff04ffff02ff36ffff04ff02ffff04ff819fff80808080ffff04ffff02ff36ffff04ff02ffff04ff13ff80808080ff8080808080808080ffff04ffff02ff36ffff04ff02ffff04ff09ff80808080ff808080808080ffff012480ff808080ff8202ff80ff8080808080808080808080ffff01ff088080ff0180ffff018202ff80ff0180ffffff0bff12ffff0bff2cff3880ffff0bff12ffff0bff12ffff0bff2cff3c80ff0580ffff0bff12ffff02ff2affff04ff02ffff04ff07ffff04ffff0bff2cff2c80ff8080808080ffff0bff2cff8080808080ff02ffff03ffff07ff0580ffff01ff0bffff0102ffff02ff36ffff04ff02ffff04ff09ff80808080ffff02ff36ffff04ff02ffff04ff0dff8080808080ffff01ff0bffff0101ff058080ff0180ffff02ffff03ff1bffff01ff02ff2effff04ff02ffff04ffff02ffff03ffff18ffff0101ff1380ffff01ff0bffff0102ff2bff0580ffff01ff0bffff0102ff05ff2b8080ff0180ffff04ffff04ffff17ff13ffff0181ff80ff3b80ff8080808080ffff010580ff0180ff02ffff03ff17ffff01ff02ffff03ffff09ff05ffff02ff2effff04ff02ffff04ff13ffff04ff27ff808080808080ffff01ff02ff3effff04ff02ffff04ff05ffff04ff1bffff04ff37ff808080808080ffff01ff088080ff0180ffff01ff010180ff0180ff018080ffff04ffff01ff01ffff81e8ff0bffffffffa08e54f5066aa7999fc1561a56df59d11ff01f7df93cadf49a61adebf65dec65ea80ffa057bfd1cb0adda3d94315053fda723f2028320faa8338225d99f629e3d46d43a980ff808080ffff33ffa0ca77e42ac3b3375edc54af271f21d075afd02d72969cababeec63e22f7ab10deff01ffffa0a14daf55d41ced6419bcd011fbc1f74ab9567fe55340d88435aa6493d628fa47ffa08e54f5066aa7999fc1561a56df59d11ff01f7df93cadf49a61adebf65dec65eaffa0c842b1a384b8633ac25d0f12bd7b614f86a77642ab6426418750f2b0b86bab2a8080ffff3fffa0bd7aa54c5f93ef1738439aa60b471ce2aa4c62fb18a7943aa10061f00dbdb8368080ffff04ffff01ffffa07faa3253bfddd1e0decb0906b2dc6247bbc4cf608f58345d173adb63e8b47c9fffa07acfcbd1ed73bfe2b698508f4ea5ed353c60ace154360272ce91f9ab0c8423c3a0eff07522495060c066f66f32acc2a77e3a3e737aca8baea4d1a64ea4cdc13da980ffff04ffff01ffa0a04d9f57764f54a43e4030befb4d80026e870519aaa66334aef8304f5d0393c280ffff04ffff01ffffa09dd8b0a6d67ee56221d0fe6bb131eb30d17c098c7548a78a962836011ea465bb8080ff018080808080ffff80ff80ff80ff80ff8080808080ca2e21c90d263e63b73d449a3f8d57b9458846f7af27d9a61a515395fa14071ea55bba78c76265b4bac257251b1e89dd13637a7c18e8dcb03e092dfb7eb5a84a0000000000000001ff02ffff01ff02ffff01ff02ffff03ffff18ff2fff3480ffff01ff04ffff04ff20ffff04ff2fff808080ffff04ffff02ff3effff04ff02ffff04ff05ffff04ffff02ff2affff04ff02ffff04ff27ffff04ffff02ffff03ff77ffff01ff02ff36ffff04ff02ffff04ff09ffff04ff57ffff04ffff02ff2effff04ff02ffff04ff05ff80808080ff808080808080ffff011d80ff0180ffff04ffff02ffff03ff77ffff0181b7ffff015780ff0180ff808080808080ffff04ff77ff808080808080ffff02ff3affff04ff02ffff04ff05ffff04ffff02ff0bff5f80ffff01ff8080808080808080ffff01ff088080ff0180ffff04ffff01ffffffff4947ff0233ffff0401ff0102ffffff20ff02ffff03ff05ffff01ff02ff32ffff04ff02ffff04ff0dffff04ffff0bff3cffff0bff34ff2480ffff0bff3cffff0bff3cffff0bff34ff2c80ff0980ffff0bff3cff0bffff0bff34ff8080808080ff8080808080ffff010b80ff0180ffff02ffff03ffff22ffff09ffff0dff0580ff2280ffff09ffff0dff0b80ff2280ffff15ff17ffff0181ff8080ffff01ff0bff05ff0bff1780ffff01ff088080ff0180ff02ffff03ff0bffff01ff02ffff03ffff02ff26ffff04ff02ffff04ff13ff80808080ffff01ff02ffff03ffff20ff1780ffff01ff02ffff03ffff09ff81b3ffff01818f80ffff01ff02ff3affff04ff02ffff04ff05ffff04ff1bffff04ff34ff808080808080ffff01ff04ffff04ff23ffff04ffff02ff36ffff04ff02ffff04ff09ffff04ff53ffff04ffff02ff2effff04ff02ffff04ff05ff80808080ff808080808080ff738080ffff02ff3affff04ff02ffff04ff05ffff04ff1bffff04ff34ff8080808080808080ff0180ffff01ff088080ff0180ffff01ff04ff13ffff02ff3affff04ff02ffff04ff05ffff04ff1bffff04ff17ff8080808080808080ff0180ffff01ff02ffff03ff17ff80ffff01ff088080ff018080ff0180ffffff02ffff03ffff09ff09ff3880ffff01ff02ffff03ffff18ff2dffff010180ffff01ff0101ff8080ff0180ff8080ff0180ff0bff3cffff0bff34ff2880ffff0bff3cffff0bff3cffff0bff34ff2c80ff0580ffff0bff3cffff02ff32ffff04ff02ffff04ff07ffff04ffff0bff34ff3480ff8080808080ffff0bff34ff8080808080ffff02ffff03ffff07ff0580ffff01ff0bffff0102ffff02ff2effff04ff02ffff04ff09ff80808080ffff02ff2effff04ff02ffff04ff0dff8080808080ffff01ff0bffff0101ff058080ff0180ff02ffff03ffff21ff17ffff09ff0bff158080ffff01ff04ff30ffff04ff0bff808080ffff01ff088080ff0180ff018080ffff04ffff01ffa07faa3253bfddd1e0decb0906b2dc6247bbc4cf608f58345d173adb63e8b47c9fffa0a14daf55d41ced6419bcd011fbc1f74ab9567fe55340d88435aa6493d628fa47a0eff07522495060c066f66f32acc2a77e3a3e737aca8baea4d1a64ea4cdc13da9ffff04ffff01ff02ffff01ff02ffff01ff02ff3effff04ff02ffff04ff05ffff04ffff02ff2fff5f80ffff04ff80ffff04ffff04ffff04ff0bffff04ff17ff808080ffff01ff808080ffff01ff8080808080808080ffff04ffff01ffffff0233ff04ff0101ffff02ff02ffff03ff05ffff01ff02ff1affff04ff02ffff04ff0dffff04ffff0bff12ffff0bff2cff1480ffff0bff12ffff0bff12ffff0bff2cff3c80ff0980ffff0bff12ff0bffff0bff2cff8080808080ff8080808080ffff010b80ff0180ffff0bff12ffff0bff2cff1080ffff0bff12ffff0bff12ffff0bff2cff3c80ff0580ffff0bff12ffff02ff1affff04ff02ffff04ff07ffff04ffff0bff2cff2c80ff8080808080ffff0bff2cff8080808080ffff02ffff03ffff07ff0580ffff01ff0bffff0102ffff02ff2effff04ff02ffff04ff09ff80808080ffff02ff2effff04ff02ffff04ff0dff8080808080ffff01ff0bffff0101ff058080ff0180ff02ffff03ff0bffff01ff02ffff03ffff09ff23ff1880ffff01ff02ffff03ffff18ff81b3ff2c80ffff01ff02ffff03ffff20ff1780ffff01ff02ff3effff04ff02ffff04ff05ffff04ff1bffff04ff33ffff04ff2fffff04ff5fff8080808080808080ffff01ff088080ff0180ffff01ff04ff13ffff02ff3effff04ff02ffff04ff05ffff04ff1bffff04ff17ffff04ff2fffff04ff5fff80808080808080808080ff0180ffff01ff02ffff03ffff09ff23ffff0181e880ffff01ff02ff3effff04ff02ffff04ff05ffff04ff1bffff04ff17ffff04ffff02ffff03ffff22ffff09ffff02ff2effff04ff02ffff04ff53ff80808080ff82014f80ffff20ff5f8080ffff01ff02ff53ffff04ff818fffff04ff82014fffff04ff81b3ff8080808080ffff01ff088080ff0180ffff04ff2cff8080808080808080ffff01ff04ff13ffff02ff3effff04ff02ffff04ff05ffff04ff1bffff04ff17ffff04ff2fffff04ff5fff80808080808080808080ff018080ff0180ffff01ff04ffff04ff18ffff04ffff02ff16ffff04ff02ffff04ff05ffff04ff27ffff04ffff0bff2cff82014f80ffff04ffff02ff2effff04ff02ffff04ff818fff80808080ffff04ffff0bff2cff0580ff8080808080808080ff378080ff81af8080ff0180ff018080ffff04ffff01a0a04d9f57764f54a43e4030befb4d80026e870519aaa66334aef8304f5d0393c2ffff04ffff01ffa08e54f5066aa7999fc1561a56df59d11ff01f7df93cadf49a61adebf65dec65ea80ffff04ffff01a057bfd1cb0adda3d94315053fda723f2028320faa8338225d99f629e3d46d43a9ffff04ffff01ff01ffff33ffa0c842b1a384b8633ac25d0f12bd7b614f86a77642ab6426418750f2b0b86bab2aff01ffffa0a14daf55d41ced6419bcd011fbc1f74ab9567fe55340d88435aa6493d628fa47ffa08e54f5066aa7999fc1561a56df59d11ff01f7df93cadf49a61adebf65dec65eaffa0c842b1a384b8633ac25d0f12bd7b614f86a77642ab6426418750f2b0b86bab2a8080ffff3eff248080ff018080808080ff01808080ffffa032dbe6d545f24635c7871ea53c623c358d7cea8f5e27a983ba6e5c0bf35fa243ffa08c4aebb18e8ce08405083c3d90a29f30239865142e2dcbca5393f40df9e3821dff0180ff01ffff8080808f4d91e1df769ca9a37dd036e1d35562d1642e9c2641a35e72a91b660187d5fe3d9a772031da55792b2c7f6b36fc3baa0f7f64a1fe9ed3a949d495d47c3f6fa0d5189cb9f1fb5e3087efebad81180c68a2e0a09eed5bdfacddddb43c5ca253d3",  # noqa: E501
        "taker": [
            {
                "store_id": "7acfcbd1ed73bfe2b698508f4ea5ed353c60ace154360272ce91f9ab0c8423c3",
                "inclusions": [{"key": "09", "value": "0209"}],
            }
        ],
        "maker": [
            {
                "store_id": "a14daf55d41ced6419bcd011fbc1f74ab9567fe55340d88435aa6493d628fa47",
                "proofs": [
                    {
                        "key": "10",
                        "value": "0110",
                        "node_hash": "de4ec93c032f5117d8af076dfc86faa5987a6c0b1d52ffc9cf0dfa43989d8c58",
                        "layers": [
                            {
                                "other_hash_side": "left",
                                "other_hash": "1c8ab812b97f5a9da0ba4be2380104810fe5c8022efe9b9e2c9d188fc3537434",
                                "combined_hash": "d340000b3a6717a5a8d42b24516ff69430235c771f8a527554b357b7f03c6de0",
                            },
                            {
                                "other_hash_side": "right",
                                "other_hash": "54e8b4cac761778f396840b343c0f1cb0e1fd0c9927d48d2f0d09a7a6f225126",
                                "combined_hash": "7676004a15439e4e8345d0f9f3a15500805b3447285904b7bcd7d14e27381d2e",
                            },
                            {
                                "other_hash_side": "right",
                                "other_hash": "6a37ca2d9a37a50f2d53387c3cf31395c72d75b1aacfa4402c32dc6d354542b4",
                                "combined_hash": "b1dc97f797a32631483c11d33b4759f5b498b512b7436286d1dc00bb1024b7e2",
                            },
                            {
                                "other_hash_side": "right",
                                "other_hash": "bcff6f16886339a196a2f6c842ad6d350a8579d123eb8602a0a85965ba25d671",
                                "combined_hash": "8e54f5066aa7999fc1561a56df59d11ff01f7df93cadf49a61adebf65dec65ea",
                            },
                        ],
                    }
                ],
            }
        ],
    },
    maker_inclusions=[{"key": b"\x10".hex(), "value": b"\x01\x10".hex()}],
    taker_inclusions=[{"key": b"\x09".hex(), "value": b"\x02\x09".hex()}],
    trade_id="5f34b109750cfaa11227228410c2114a1a45e3916e90b648fe9a02137dd89d22",
    maker_root_history=[
        bytes32.from_hexstr("6661ea6604b491118b0f49c932c0f0de2ad815a57b54b6ec8fdbd1b408ae7e27"),
        bytes32.from_hexstr("8e54f5066aa7999fc1561a56df59d11ff01f7df93cadf49a61adebf65dec65ea"),
    ],
    taker_root_history=[
        bytes32.from_hexstr("42f08ebc0578f2cec7a9ad1c3038e74e0f30eba5c2f4cb1ee1c8fdb682c19dbb"),
    ],
)


make_one_upsert_take_one_reference = MakeAndTakeReference(
    entries_to_insert=10,
    make_offer_response={
        "trade_id": "51ddb0b38d6253f437ceef26edcad140ffb7e97700007e859a9b86a005a69eb7",
        "offer": "0000000300000000000000000000000000000000000000000000000000000000000000002a5e6ce75ee92c329c576b5dfe550bd8a9054a7b3da5c5f74b1c2c55417fc05f0000000000000000ff02ffff01ff02ffff01ff02ffff03ffff18ff2fff3480ffff01ff04ffff04ff20ffff04ff2fff808080ffff04ffff02ff3effff04ff02ffff04ff05ffff04ffff02ff2affff04ff02ffff04ff27ffff04ffff02ffff03ff77ffff01ff02ff36ffff04ff02ffff04ff09ffff04ff57ffff04ffff02ff2effff04ff02ffff04ff05ff80808080ff808080808080ffff011d80ff0180ffff04ffff02ffff03ff77ffff0181b7ffff015780ff0180ff808080808080ffff04ff77ff808080808080ffff02ff3affff04ff02ffff04ff05ffff04ffff02ff0bff5f80ffff01ff8080808080808080ffff01ff088080ff0180ffff04ffff01ffffffff4947ff0233ffff0401ff0102ffffff20ff02ffff03ff05ffff01ff02ff32ffff04ff02ffff04ff0dffff04ffff0bff3cffff0bff34ff2480ffff0bff3cffff0bff3cffff0bff34ff2c80ff0980ffff0bff3cff0bffff0bff34ff8080808080ff8080808080ffff010b80ff0180ffff02ffff03ffff22ffff09ffff0dff0580ff2280ffff09ffff0dff0b80ff2280ffff15ff17ffff0181ff8080ffff01ff0bff05ff0bff1780ffff01ff088080ff0180ff02ffff03ff0bffff01ff02ffff03ffff02ff26ffff04ff02ffff04ff13ff80808080ffff01ff02ffff03ffff20ff1780ffff01ff02ffff03ffff09ff81b3ffff01818f80ffff01ff02ff3affff04ff02ffff04ff05ffff04ff1bffff04ff34ff808080808080ffff01ff04ffff04ff23ffff04ffff02ff36ffff04ff02ffff04ff09ffff04ff53ffff04ffff02ff2effff04ff02ffff04ff05ff80808080ff808080808080ff738080ffff02ff3affff04ff02ffff04ff05ffff04ff1bffff04ff34ff8080808080808080ff0180ffff01ff088080ff0180ffff01ff04ff13ffff02ff3affff04ff02ffff04ff05ffff04ff1bffff04ff17ff8080808080808080ff0180ffff01ff02ffff03ff17ff80ffff01ff088080ff018080ff0180ffffff02ffff03ffff09ff09ff3880ffff01ff02ffff03ffff18ff2dffff010180ffff01ff0101ff8080ff0180ff8080ff0180ff0bff3cffff0bff34ff2880ffff0bff3cffff0bff3cffff0bff34ff2c80ff0580ffff0bff3cffff02ff32ffff04ff02ffff04ff07ffff04ffff0bff34ff3480ff8080808080ffff0bff34ff8080808080ffff02ffff03ffff07ff0580ffff01ff0bffff0102ffff02ff2effff04ff02ffff04ff09ff80808080ffff02ff2effff04ff02ffff04ff0dff8080808080ffff01ff0bffff0101ff058080ff0180ff02ffff03ffff21ff17ffff09ff0bff158080ffff01ff04ff30ffff04ff0bff808080ffff01ff088080ff0180ff018080ffff04ffff01ffa07faa3253bfddd1e0decb0906b2dc6247bbc4cf608f58345d173adb63e8b47c9fffa07acfcbd1ed73bfe2b698508f4ea5ed353c60ace154360272ce91f9ab0c8423c3a0eff07522495060c066f66f32acc2a77e3a3e737aca8baea4d1a64ea4cdc13da9ffff04ffff01ff02ffff01ff02ffff01ff02ff3effff04ff02ffff04ff05ffff04ffff02ff2fff5f80ffff04ff80ffff04ffff04ffff04ff0bffff04ff17ff808080ffff01ff808080ffff01ff8080808080808080ffff04ffff01ffffff0233ff04ff0101ffff02ff02ffff03ff05ffff01ff02ff1affff04ff02ffff04ff0dffff04ffff0bff12ffff0bff2cff1480ffff0bff12ffff0bff12ffff0bff2cff3c80ff0980ffff0bff12ff0bffff0bff2cff8080808080ff8080808080ffff010b80ff0180ffff0bff12ffff0bff2cff1080ffff0bff12ffff0bff12ffff0bff2cff3c80ff0580ffff0bff12ffff02ff1affff04ff02ffff04ff07ffff04ffff0bff2cff2c80ff8080808080ffff0bff2cff8080808080ffff02ffff03ffff07ff0580ffff01ff0bffff0102ffff02ff2effff04ff02ffff04ff09ff80808080ffff02ff2effff04ff02ffff04ff0dff8080808080ffff01ff0bffff0101ff058080ff0180ff02ffff03ff0bffff01ff02ffff03ffff09ff23ff1880ffff01ff02ffff03ffff18ff81b3ff2c80ffff01ff02ffff03ffff20ff1780ffff01ff02ff3effff04ff02ffff04ff05ffff04ff1bffff04ff33ffff04ff2fffff04ff5fff8080808080808080ffff01ff088080ff0180ffff01ff04ff13ffff02ff3effff04ff02ffff04ff05ffff04ff1bffff04ff17ffff04ff2fffff04ff5fff80808080808080808080ff0180ffff01ff02ffff03ffff09ff23ffff0181e880ffff01ff02ff3effff04ff02ffff04ff05ffff04ff1bffff04ff17ffff04ffff02ffff03ffff22ffff09ffff02ff2effff04ff02ffff04ff53ff80808080ff82014f80ffff20ff5f8080ffff01ff02ff53ffff04ff818fffff04ff82014fffff04ff81b3ff8080808080ffff01ff088080ff0180ffff04ff2cff8080808080808080ffff01ff04ff13ffff02ff3effff04ff02ffff04ff05ffff04ff1bffff04ff17ffff04ff2fffff04ff5fff80808080808080808080ff018080ff0180ffff01ff04ffff04ff18ffff04ffff02ff16ffff04ff02ffff04ff05ffff04ff27ffff04ffff0bff2cff82014f80ffff04ffff02ff2effff04ff02ffff04ff818fff80808080ffff04ffff0bff2cff0580ff8080808080808080ff378080ff81af8080ff0180ff018080ffff04ffff01a0a04d9f57764f54a43e4030befb4d80026e870519aaa66334aef8304f5d0393c2ffff04ffff01ffa042f08ebc0578f2cec7a9ad1c3038e74e0f30eba5c2f4cb1ee1c8fdb682c19dbb80ffff04ffff01a057bfd1cb0adda3d94315053fda723f2028320faa8338225d99f629e3d46d43a9ffff04ffff01ff02ffff01ff02ff0affff04ff02ffff04ff03ff80808080ffff04ffff01ffff333effff02ffff03ff05ffff01ff04ffff04ff0cffff04ffff02ff1effff04ff02ffff04ff09ff80808080ff808080ffff02ff16ffff04ff02ffff04ff19ffff04ffff02ff0affff04ff02ffff04ff0dff80808080ff808080808080ff8080ff0180ffff02ffff03ff05ffff01ff04ffff04ff08ff0980ffff02ff16ffff04ff02ffff04ff0dffff04ff0bff808080808080ffff010b80ff0180ff02ffff03ffff07ff0580ffff01ff0bffff0102ffff02ff1effff04ff02ffff04ff09ff80808080ffff02ff1effff04ff02ffff04ff0dff8080808080ffff01ff0bffff0101ff058080ff0180ff018080ff018080808080ff01808080ffffa00000000000000000000000000000000000000000000000000000000000000000ffffa00000000000000000000000000000000000000000000000000000000000000000ff01ff8080808032dbe6d545f24635c7871ea53c623c358d7cea8f5e27a983ba6e5c0bf35fa243aa064e96a86637d8f5ebe153dc8645d29f43bee762d5ec10d06c8617fa60b8c50000000000000001ff02ffff01ff02ffff01ff02ffff03ffff18ff2fff3480ffff01ff04ffff04ff20ffff04ff2fff808080ffff04ffff02ff3effff04ff02ffff04ff05ffff04ffff02ff2affff04ff02ffff04ff27ffff04ffff02ffff03ff77ffff01ff02ff36ffff04ff02ffff04ff09ffff04ff57ffff04ffff02ff2effff04ff02ffff04ff05ff80808080ff808080808080ffff011d80ff0180ffff04ffff02ffff03ff77ffff0181b7ffff015780ff0180ff808080808080ffff04ff77ff808080808080ffff02ff3affff04ff02ffff04ff05ffff04ffff02ff0bff5f80ffff01ff8080808080808080ffff01ff088080ff0180ffff04ffff01ffffffff4947ff0233ffff0401ff0102ffffff20ff02ffff03ff05ffff01ff02ff32ffff04ff02ffff04ff0dffff04ffff0bff3cffff0bff34ff2480ffff0bff3cffff0bff3cffff0bff34ff2c80ff0980ffff0bff3cff0bffff0bff34ff8080808080ff8080808080ffff010b80ff0180ffff02ffff03ffff22ffff09ffff0dff0580ff2280ffff09ffff0dff0b80ff2280ffff15ff17ffff0181ff8080ffff01ff0bff05ff0bff1780ffff01ff088080ff0180ff02ffff03ff0bffff01ff02ffff03ffff02ff26ffff04ff02ffff04ff13ff80808080ffff01ff02ffff03ffff20ff1780ffff01ff02ffff03ffff09ff81b3ffff01818f80ffff01ff02ff3affff04ff02ffff04ff05ffff04ff1bffff04ff34ff808080808080ffff01ff04ffff04ff23ffff04ffff02ff36ffff04ff02ffff04ff09ffff04ff53ffff04ffff02ff2effff04ff02ffff04ff05ff80808080ff808080808080ff738080ffff02ff3affff04ff02ffff04ff05ffff04ff1bffff04ff34ff8080808080808080ff0180ffff01ff088080ff0180ffff01ff04ff13ffff02ff3affff04ff02ffff04ff05ffff04ff1bffff04ff17ff8080808080808080ff0180ffff01ff02ffff03ff17ff80ffff01ff088080ff018080ff0180ffffff02ffff03ffff09ff09ff3880ffff01ff02ffff03ffff18ff2dffff010180ffff01ff0101ff8080ff0180ff8080ff0180ff0bff3cffff0bff34ff2880ffff0bff3cffff0bff3cffff0bff34ff2c80ff0580ffff0bff3cffff02ff32ffff04ff02ffff04ff07ffff04ffff0bff34ff3480ff8080808080ffff0bff34ff8080808080ffff02ffff03ffff07ff0580ffff01ff0bffff0102ffff02ff2effff04ff02ffff04ff09ff80808080ffff02ff2effff04ff02ffff04ff0dff8080808080ffff01ff0bffff0101ff058080ff0180ff02ffff03ffff21ff17ffff09ff0bff158080ffff01ff04ff30ffff04ff0bff808080ffff01ff088080ff0180ff018080ffff04ffff01ffa07faa3253bfddd1e0decb0906b2dc6247bbc4cf608f58345d173adb63e8b47c9fffa0a14daf55d41ced6419bcd011fbc1f74ab9567fe55340d88435aa6493d628fa47a0eff07522495060c066f66f32acc2a77e3a3e737aca8baea4d1a64ea4cdc13da9ffff04ffff01ff02ffff01ff02ffff01ff02ff3effff04ff02ffff04ff05ffff04ffff02ff2fff5f80ffff04ff80ffff04ffff04ffff04ff0bffff04ff17ff808080ffff01ff808080ffff01ff8080808080808080ffff04ffff01ffffff0233ff04ff0101ffff02ff02ffff03ff05ffff01ff02ff1affff04ff02ffff04ff0dffff04ffff0bff12ffff0bff2cff1480ffff0bff12ffff0bff12ffff0bff2cff3c80ff0980ffff0bff12ff0bffff0bff2cff8080808080ff8080808080ffff010b80ff0180ffff0bff12ffff0bff2cff1080ffff0bff12ffff0bff12ffff0bff2cff3c80ff0580ffff0bff12ffff02ff1affff04ff02ffff04ff07ffff04ffff0bff2cff2c80ff8080808080ffff0bff2cff8080808080ffff02ffff03ffff07ff0580ffff01ff0bffff0102ffff02ff2effff04ff02ffff04ff09ff80808080ffff02ff2effff04ff02ffff04ff0dff8080808080ffff01ff0bffff0101ff058080ff0180ff02ffff03ff0bffff01ff02ffff03ffff09ff23ff1880ffff01ff02ffff03ffff18ff81b3ff2c80ffff01ff02ffff03ffff20ff1780ffff01ff02ff3effff04ff02ffff04ff05ffff04ff1bffff04ff33ffff04ff2fffff04ff5fff8080808080808080ffff01ff088080ff0180ffff01ff04ff13ffff02ff3effff04ff02ffff04ff05ffff04ff1bffff04ff17ffff04ff2fffff04ff5fff80808080808080808080ff0180ffff01ff02ffff03ffff09ff23ffff0181e880ffff01ff02ff3effff04ff02ffff04ff05ffff04ff1bffff04ff17ffff04ffff02ffff03ffff22ffff09ffff02ff2effff04ff02ffff04ff53ff80808080ff82014f80ffff20ff5f8080ffff01ff02ff53ffff04ff818fffff04ff82014fffff04ff81b3ff8080808080ffff01ff088080ff0180ffff04ff2cff8080808080808080ffff01ff04ff13ffff02ff3effff04ff02ffff04ff05ffff04ff1bffff04ff17ffff04ff2fffff04ff5fff80808080808080808080ff018080ff0180ffff01ff04ffff04ff18ffff04ffff02ff16ffff04ff02ffff04ff05ffff04ff27ffff04ffff0bff2cff82014f80ffff04ffff02ff2effff04ff02ffff04ff818fff80808080ffff04ffff0bff2cff0580ff8080808080808080ff378080ff81af8080ff0180ff018080ffff04ffff01a0a04d9f57764f54a43e4030befb4d80026e870519aaa66334aef8304f5d0393c2ffff04ffff01ffa06661ea6604b491118b0f49c932c0f0de2ad815a57b54b6ec8fdbd1b408ae7e2780ffff04ffff01a057bfd1cb0adda3d94315053fda723f2028320faa8338225d99f629e3d46d43a9ffff04ffff01ff02ffff01ff02ffff01ff02ffff03ff0bffff01ff02ffff03ffff09ff05ffff1dff0bffff1effff0bff0bffff02ff06ffff04ff02ffff04ff17ff8080808080808080ffff01ff02ff17ff2f80ffff01ff088080ff0180ffff01ff04ffff04ff04ffff04ff05ffff04ffff02ff06ffff04ff02ffff04ff17ff80808080ff80808080ffff02ff17ff2f808080ff0180ffff04ffff01ff32ff02ffff03ffff07ff0580ffff01ff0bffff0102ffff02ff06ffff04ff02ffff04ff09ff80808080ffff02ff06ffff04ff02ffff04ff0dff8080808080ffff01ff0bffff0101ff058080ff0180ff018080ffff04ffff01b0a132fae32c98cbb7d8f5814c49ee3f0ba6ec2172c5e5f6900655a65cd2157a06a1c6eb89c68c8d2cdcee9506c2217978ff018080ff018080808080ff01808080ffffa0a14daf55d41ced6419bcd011fbc1f74ab9567fe55340d88435aa6493d628fa47ffa01804338c97f989c78d88716206c0f27315f3eb7d59417ab2eacee20f0a7ff60bff0180ff01ffffff80ffff02ffff01ff02ffff01ff02ffff03ff5fffff01ff02ff3affff04ff02ffff04ff0bffff04ff17ffff04ff2fffff04ff5fffff04ff81bfffff04ff82017fffff04ff8202ffffff04ffff02ff05ff8205ff80ff8080808080808080808080ffff01ff04ffff04ff10ffff01ff81ff8080ffff02ff05ff8205ff808080ff0180ffff04ffff01ffffff49ff3f02ff04ff0101ffff02ffff02ffff03ff05ffff01ff02ff2affff04ff02ffff04ff0dffff04ffff0bff12ffff0bff2cff1480ffff0bff12ffff0bff12ffff0bff2cff3c80ff0980ffff0bff12ff0bffff0bff2cff8080808080ff8080808080ffff010b80ff0180ff02ffff03ff05ffff01ff02ffff03ffff02ff3effff04ff02ffff04ff82011fffff04ff27ffff04ff4fff808080808080ffff01ff02ff3affff04ff02ffff04ff0dffff04ff1bffff04ff37ffff04ff6fffff04ff81dfffff04ff8201bfffff04ff82037fffff04ffff04ffff04ff28ffff04ffff0bffff02ff26ffff04ff02ffff04ff11ffff04ffff02ff26ffff04ff02ffff04ff13ffff04ff82027fffff04ffff02ff36ffff04ff02ffff04ff82013fff80808080ffff04ffff02ff36ffff04ff02ffff04ff819fff80808080ffff04ffff02ff36ffff04ff02ffff04ff13ff80808080ff8080808080808080ffff04ffff02ff36ffff04ff02ffff04ff09ff80808080ff808080808080ffff012480ff808080ff8202ff80ff8080808080808080808080ffff01ff088080ff0180ffff018202ff80ff0180ffffff0bff12ffff0bff2cff3880ffff0bff12ffff0bff12ffff0bff2cff3c80ff0580ffff0bff12ffff02ff2affff04ff02ffff04ff07ffff04ffff0bff2cff2c80ff8080808080ffff0bff2cff8080808080ff02ffff03ffff07ff0580ffff01ff0bffff0102ffff02ff36ffff04ff02ffff04ff09ff80808080ffff02ff36ffff04ff02ffff04ff0dff8080808080ffff01ff0bffff0101ff058080ff0180ffff02ffff03ff1bffff01ff02ff2effff04ff02ffff04ffff02ffff03ffff18ffff0101ff1380ffff01ff0bffff0102ff2bff0580ffff01ff0bffff0102ff05ff2b8080ff0180ffff04ffff04ffff17ff13ffff0181ff80ff3b80ff8080808080ffff010580ff0180ff02ffff03ff17ffff01ff02ffff03ffff09ff05ffff02ff2effff04ff02ffff04ff13ffff04ff27ff808080808080ffff01ff02ff3effff04ff02ffff04ff05ffff04ff1bffff04ff37ff808080808080ffff01ff088080ff0180ffff01ff010180ff0180ff018080ffff04ffff01ff01ffff81e8ff0bffffffffa03761921b9b0520458995bb0ec353ea28d36efa2a7cfc3aba6772f005f7dd34c680ffa057bfd1cb0adda3d94315053fda723f2028320faa8338225d99f629e3d46d43a980ff808080ffff33ffa05ef937e981ce68f2fa71e00b139acb3352b5ec32e7d6bc160874a456f106016cff01ffffa0a14daf55d41ced6419bcd011fbc1f74ab9567fe55340d88435aa6493d628fa47ffa03761921b9b0520458995bb0ec353ea28d36efa2a7cfc3aba6772f005f7dd34c6ffa0c842b1a384b8633ac25d0f12bd7b614f86a77642ab6426418750f2b0b86bab2a8080ffff3fffa0214dc115c3f3a3444619449b297fe03521f85c8cc12be80d8de35bb9cfb29e6d8080ffff04ffff01ffffa07faa3253bfddd1e0decb0906b2dc6247bbc4cf608f58345d173adb63e8b47c9fffa07acfcbd1ed73bfe2b698508f4ea5ed353c60ace154360272ce91f9ab0c8423c3a0eff07522495060c066f66f32acc2a77e3a3e737aca8baea4d1a64ea4cdc13da980ffff04ffff01ffa0a04d9f57764f54a43e4030befb4d80026e870519aaa66334aef8304f5d0393c280ffff04ffff01ffffa07f3e180acdf046f955d3440bb3a16dfd6f5a46c809cee98e7514127327b1cab58080ff018080808080ffff80ff80ff80ff80ff8080808080ca2e21c90d263e63b73d449a3f8d57b9458846f7af27d9a61a515395fa14071e3a306420fb91e6a9c25cb93938b5c9e164ce761abc2ab967f8545cdcdc9e6e6d0000000000000001ff02ffff01ff02ffff01ff02ffff03ffff18ff2fff3480ffff01ff04ffff04ff20ffff04ff2fff808080ffff04ffff02ff3effff04ff02ffff04ff05ffff04ffff02ff2affff04ff02ffff04ff27ffff04ffff02ffff03ff77ffff01ff02ff36ffff04ff02ffff04ff09ffff04ff57ffff04ffff02ff2effff04ff02ffff04ff05ff80808080ff808080808080ffff011d80ff0180ffff04ffff02ffff03ff77ffff0181b7ffff015780ff0180ff808080808080ffff04ff77ff808080808080ffff02ff3affff04ff02ffff04ff05ffff04ffff02ff0bff5f80ffff01ff8080808080808080ffff01ff088080ff0180ffff04ffff01ffffffff4947ff0233ffff0401ff0102ffffff20ff02ffff03ff05ffff01ff02ff32ffff04ff02ffff04ff0dffff04ffff0bff3cffff0bff34ff2480ffff0bff3cffff0bff3cffff0bff34ff2c80ff0980ffff0bff3cff0bffff0bff34ff8080808080ff8080808080ffff010b80ff0180ffff02ffff03ffff22ffff09ffff0dff0580ff2280ffff09ffff0dff0b80ff2280ffff15ff17ffff0181ff8080ffff01ff0bff05ff0bff1780ffff01ff088080ff0180ff02ffff03ff0bffff01ff02ffff03ffff02ff26ffff04ff02ffff04ff13ff80808080ffff01ff02ffff03ffff20ff1780ffff01ff02ffff03ffff09ff81b3ffff01818f80ffff01ff02ff3affff04ff02ffff04ff05ffff04ff1bffff04ff34ff808080808080ffff01ff04ffff04ff23ffff04ffff02ff36ffff04ff02ffff04ff09ffff04ff53ffff04ffff02ff2effff04ff02ffff04ff05ff80808080ff808080808080ff738080ffff02ff3affff04ff02ffff04ff05ffff04ff1bffff04ff34ff8080808080808080ff0180ffff01ff088080ff0180ffff01ff04ff13ffff02ff3affff04ff02ffff04ff05ffff04ff1bffff04ff17ff8080808080808080ff0180ffff01ff02ffff03ff17ff80ffff01ff088080ff018080ff0180ffffff02ffff03ffff09ff09ff3880ffff01ff02ffff03ffff18ff2dffff010180ffff01ff0101ff8080ff0180ff8080ff0180ff0bff3cffff0bff34ff2880ffff0bff3cffff0bff3cffff0bff34ff2c80ff0580ffff0bff3cffff02ff32ffff04ff02ffff04ff07ffff04ffff0bff34ff3480ff8080808080ffff0bff34ff8080808080ffff02ffff03ffff07ff0580ffff01ff0bffff0102ffff02ff2effff04ff02ffff04ff09ff80808080ffff02ff2effff04ff02ffff04ff0dff8080808080ffff01ff0bffff0101ff058080ff0180ff02ffff03ffff21ff17ffff09ff0bff158080ffff01ff04ff30ffff04ff0bff808080ffff01ff088080ff0180ff018080ffff04ffff01ffa07faa3253bfddd1e0decb0906b2dc6247bbc4cf608f58345d173adb63e8b47c9fffa0a14daf55d41ced6419bcd011fbc1f74ab9567fe55340d88435aa6493d628fa47a0eff07522495060c066f66f32acc2a77e3a3e737aca8baea4d1a64ea4cdc13da9ffff04ffff01ff02ffff01ff02ffff01ff02ff3effff04ff02ffff04ff05ffff04ffff02ff2fff5f80ffff04ff80ffff04ffff04ffff04ff0bffff04ff17ff808080ffff01ff808080ffff01ff8080808080808080ffff04ffff01ffffff0233ff04ff0101ffff02ff02ffff03ff05ffff01ff02ff1affff04ff02ffff04ff0dffff04ffff0bff12ffff0bff2cff1480ffff0bff12ffff0bff12ffff0bff2cff3c80ff0980ffff0bff12ff0bffff0bff2cff8080808080ff8080808080ffff010b80ff0180ffff0bff12ffff0bff2cff1080ffff0bff12ffff0bff12ffff0bff2cff3c80ff0580ffff0bff12ffff02ff1affff04ff02ffff04ff07ffff04ffff0bff2cff2c80ff8080808080ffff0bff2cff8080808080ffff02ffff03ffff07ff0580ffff01ff0bffff0102ffff02ff2effff04ff02ffff04ff09ff80808080ffff02ff2effff04ff02ffff04ff0dff8080808080ffff01ff0bffff0101ff058080ff0180ff02ffff03ff0bffff01ff02ffff03ffff09ff23ff1880ffff01ff02ffff03ffff18ff81b3ff2c80ffff01ff02ffff03ffff20ff1780ffff01ff02ff3effff04ff02ffff04ff05ffff04ff1bffff04ff33ffff04ff2fffff04ff5fff8080808080808080ffff01ff088080ff0180ffff01ff04ff13ffff02ff3effff04ff02ffff04ff05ffff04ff1bffff04ff17ffff04ff2fffff04ff5fff80808080808080808080ff0180ffff01ff02ffff03ffff09ff23ffff0181e880ffff01ff02ff3effff04ff02ffff04ff05ffff04ff1bffff04ff17ffff04ffff02ffff03ffff22ffff09ffff02ff2effff04ff02ffff04ff53ff80808080ff82014f80ffff20ff5f8080ffff01ff02ff53ffff04ff818fffff04ff82014fffff04ff81b3ff8080808080ffff01ff088080ff0180ffff04ff2cff8080808080808080ffff01ff04ff13ffff02ff3effff04ff02ffff04ff05ffff04ff1bffff04ff17ffff04ff2fffff04ff5fff80808080808080808080ff018080ff0180ffff01ff04ffff04ff18ffff04ffff02ff16ffff04ff02ffff04ff05ffff04ff27ffff04ffff0bff2cff82014f80ffff04ffff02ff2effff04ff02ffff04ff818fff80808080ffff04ffff0bff2cff0580ff8080808080808080ff378080ff81af8080ff0180ff018080ffff04ffff01a0a04d9f57764f54a43e4030befb4d80026e870519aaa66334aef8304f5d0393c2ffff04ffff01ffa03761921b9b0520458995bb0ec353ea28d36efa2a7cfc3aba6772f005f7dd34c680ffff04ffff01a057bfd1cb0adda3d94315053fda723f2028320faa8338225d99f629e3d46d43a9ffff04ffff01ff01ffff33ffa0c842b1a384b8633ac25d0f12bd7b614f86a77642ab6426418750f2b0b86bab2aff01ffffa0a14daf55d41ced6419bcd011fbc1f74ab9567fe55340d88435aa6493d628fa47ffa03761921b9b0520458995bb0ec353ea28d36efa2a7cfc3aba6772f005f7dd34c6ffa0c842b1a384b8633ac25d0f12bd7b614f86a77642ab6426418750f2b0b86bab2a8080ffff3eff248080ff018080808080ff01808080ffffa032dbe6d545f24635c7871ea53c623c358d7cea8f5e27a983ba6e5c0bf35fa243ffa08c4aebb18e8ce08405083c3d90a29f30239865142e2dcbca5393f40df9e3821dff0180ff01ffff808080a8cc33305b54b3076148bd51437107e3d07be05db639576096847bb67d90a460d8e0788ef9e86873bfdb25193c09c192054bbb4e8a1e933601e2c5f5b59a28654f850bcfeac726ef3c0a70ab794aff3309e5f987937995bd163366a3d2ca9877",  # noqa: E501
        "taker": [
            {
                "store_id": "7acfcbd1ed73bfe2b698508f4ea5ed353c60ace154360272ce91f9ab0c8423c3",
                "inclusions": [{"key": "10", "value": "0210"}],
            }
        ],
        "maker": [
            {
                "store_id": "a14daf55d41ced6419bcd011fbc1f74ab9567fe55340d88435aa6493d628fa47",
                "proofs": [
                    {
                        "key": "09",
                        "value": "0110",
                        "node_hash": "537527cd8d1ba52f94be6adde14400becd977f0a8cdcee17b10e74d408a64af8",
                        "layers": [
                            {
                                "other_hash_side": "right",
                                "other_hash": "1c8ab812b97f5a9da0ba4be2380104810fe5c8022efe9b9e2c9d188fc3537434",
                                "combined_hash": "a642d1018f3ff35a6f693407cb9860cfb7a8f969d356e7dda0ef8c89a61060b7",
                            },
                            {
                                "other_hash_side": "right",
                                "other_hash": "54e8b4cac761778f396840b343c0f1cb0e1fd0c9927d48d2f0d09a7a6f225126",
                                "combined_hash": "d1d6e6f1f4e5d776405fbf98872075c3434462ebf8d139880f28dd6e42aece90",
                            },
                            {
                                "other_hash_side": "right",
                                "other_hash": "6a37ca2d9a37a50f2d53387c3cf31395c72d75b1aacfa4402c32dc6d354542b4",
                                "combined_hash": "80f288f1fb9feafaa53de8b54622ef2f2532aa6422081ac143540c36d9a2bde2",
                            },
                            {
                                "other_hash_side": "right",
                                "other_hash": "ca35e1f8ddc62f809f8b4c44a965273eb88cb720add4ba4b03c9865b70ef18a2",
                                "combined_hash": "3761921b9b0520458995bb0ec353ea28d36efa2a7cfc3aba6772f005f7dd34c6",
                            },
                        ],
                    }
                ],
            }
        ],
    },
    maker_inclusions=[{"key": b"\x09".hex(), "value": b"\x01\x10".hex()}],
    taker_inclusions=[{"key": b"\x10".hex(), "value": b"\x02\x10".hex()}],
    trade_id="8b9070182a498ab2f8d48f3ba9df86e34c91c56942589695aea8835ad871c69c",
    maker_root_history=[
        bytes32.from_hexstr("6661ea6604b491118b0f49c932c0f0de2ad815a57b54b6ec8fdbd1b408ae7e27"),
        bytes32.from_hexstr("3761921b9b0520458995bb0ec353ea28d36efa2a7cfc3aba6772f005f7dd34c6"),
    ],
    taker_root_history=[
        bytes32.from_hexstr("42f08ebc0578f2cec7a9ad1c3038e74e0f30eba5c2f4cb1ee1c8fdb682c19dbb"),
        bytes32.from_hexstr("eeb63ac765065d2ee161e1c059c8188ef809e1c3ed8739bad5bfee2c2ee1c742"),
    ],
)


make_one_take_one_upsert_reference = MakeAndTakeReference(
    entries_to_insert=10,
    make_offer_response={
        "trade_id": "47a6b49445205f771cffbbb9f758384dfec55958069602e86e2578e87bf10029",
        "offer": "0000000300000000000000000000000000000000000000000000000000000000000000002a5e6ce75ee92c329c576b5dfe550bd8a9054a7b3da5c5f74b1c2c55417fc05f0000000000000000ff02ffff01ff02ffff01ff02ffff03ffff18ff2fff3480ffff01ff04ffff04ff20ffff04ff2fff808080ffff04ffff02ff3effff04ff02ffff04ff05ffff04ffff02ff2affff04ff02ffff04ff27ffff04ffff02ffff03ff77ffff01ff02ff36ffff04ff02ffff04ff09ffff04ff57ffff04ffff02ff2effff04ff02ffff04ff05ff80808080ff808080808080ffff011d80ff0180ffff04ffff02ffff03ff77ffff0181b7ffff015780ff0180ff808080808080ffff04ff77ff808080808080ffff02ff3affff04ff02ffff04ff05ffff04ffff02ff0bff5f80ffff01ff8080808080808080ffff01ff088080ff0180ffff04ffff01ffffffff4947ff0233ffff0401ff0102ffffff20ff02ffff03ff05ffff01ff02ff32ffff04ff02ffff04ff0dffff04ffff0bff3cffff0bff34ff2480ffff0bff3cffff0bff3cffff0bff34ff2c80ff0980ffff0bff3cff0bffff0bff34ff8080808080ff8080808080ffff010b80ff0180ffff02ffff03ffff22ffff09ffff0dff0580ff2280ffff09ffff0dff0b80ff2280ffff15ff17ffff0181ff8080ffff01ff0bff05ff0bff1780ffff01ff088080ff0180ff02ffff03ff0bffff01ff02ffff03ffff02ff26ffff04ff02ffff04ff13ff80808080ffff01ff02ffff03ffff20ff1780ffff01ff02ffff03ffff09ff81b3ffff01818f80ffff01ff02ff3affff04ff02ffff04ff05ffff04ff1bffff04ff34ff808080808080ffff01ff04ffff04ff23ffff04ffff02ff36ffff04ff02ffff04ff09ffff04ff53ffff04ffff02ff2effff04ff02ffff04ff05ff80808080ff808080808080ff738080ffff02ff3affff04ff02ffff04ff05ffff04ff1bffff04ff34ff8080808080808080ff0180ffff01ff088080ff0180ffff01ff04ff13ffff02ff3affff04ff02ffff04ff05ffff04ff1bffff04ff17ff8080808080808080ff0180ffff01ff02ffff03ff17ff80ffff01ff088080ff018080ff0180ffffff02ffff03ffff09ff09ff3880ffff01ff02ffff03ffff18ff2dffff010180ffff01ff0101ff8080ff0180ff8080ff0180ff0bff3cffff0bff34ff2880ffff0bff3cffff0bff3cffff0bff34ff2c80ff0580ffff0bff3cffff02ff32ffff04ff02ffff04ff07ffff04ffff0bff34ff3480ff8080808080ffff0bff34ff8080808080ffff02ffff03ffff07ff0580ffff01ff0bffff0102ffff02ff2effff04ff02ffff04ff09ff80808080ffff02ff2effff04ff02ffff04ff0dff8080808080ffff01ff0bffff0101ff058080ff0180ff02ffff03ffff21ff17ffff09ff0bff158080ffff01ff04ff30ffff04ff0bff808080ffff01ff088080ff0180ff018080ffff04ffff01ffa07faa3253bfddd1e0decb0906b2dc6247bbc4cf608f58345d173adb63e8b47c9fffa07acfcbd1ed73bfe2b698508f4ea5ed353c60ace154360272ce91f9ab0c8423c3a0eff07522495060c066f66f32acc2a77e3a3e737aca8baea4d1a64ea4cdc13da9ffff04ffff01ff02ffff01ff02ffff01ff02ff3effff04ff02ffff04ff05ffff04ffff02ff2fff5f80ffff04ff80ffff04ffff04ffff04ff0bffff04ff17ff808080ffff01ff808080ffff01ff8080808080808080ffff04ffff01ffffff0233ff04ff0101ffff02ff02ffff03ff05ffff01ff02ff1affff04ff02ffff04ff0dffff04ffff0bff12ffff0bff2cff1480ffff0bff12ffff0bff12ffff0bff2cff3c80ff0980ffff0bff12ff0bffff0bff2cff8080808080ff8080808080ffff010b80ff0180ffff0bff12ffff0bff2cff1080ffff0bff12ffff0bff12ffff0bff2cff3c80ff0580ffff0bff12ffff02ff1affff04ff02ffff04ff07ffff04ffff0bff2cff2c80ff8080808080ffff0bff2cff8080808080ffff02ffff03ffff07ff0580ffff01ff0bffff0102ffff02ff2effff04ff02ffff04ff09ff80808080ffff02ff2effff04ff02ffff04ff0dff8080808080ffff01ff0bffff0101ff058080ff0180ff02ffff03ff0bffff01ff02ffff03ffff09ff23ff1880ffff01ff02ffff03ffff18ff81b3ff2c80ffff01ff02ffff03ffff20ff1780ffff01ff02ff3effff04ff02ffff04ff05ffff04ff1bffff04ff33ffff04ff2fffff04ff5fff8080808080808080ffff01ff088080ff0180ffff01ff04ff13ffff02ff3effff04ff02ffff04ff05ffff04ff1bffff04ff17ffff04ff2fffff04ff5fff80808080808080808080ff0180ffff01ff02ffff03ffff09ff23ffff0181e880ffff01ff02ff3effff04ff02ffff04ff05ffff04ff1bffff04ff17ffff04ffff02ffff03ffff22ffff09ffff02ff2effff04ff02ffff04ff53ff80808080ff82014f80ffff20ff5f8080ffff01ff02ff53ffff04ff818fffff04ff82014fffff04ff81b3ff8080808080ffff01ff088080ff0180ffff04ff2cff8080808080808080ffff01ff04ff13ffff02ff3effff04ff02ffff04ff05ffff04ff1bffff04ff17ffff04ff2fffff04ff5fff80808080808080808080ff018080ff0180ffff01ff04ffff04ff18ffff04ffff02ff16ffff04ff02ffff04ff05ffff04ff27ffff04ffff0bff2cff82014f80ffff04ffff02ff2effff04ff02ffff04ff818fff80808080ffff04ffff0bff2cff0580ff8080808080808080ff378080ff81af8080ff0180ff018080ffff04ffff01a0a04d9f57764f54a43e4030befb4d80026e870519aaa66334aef8304f5d0393c2ffff04ffff01ffa042f08ebc0578f2cec7a9ad1c3038e74e0f30eba5c2f4cb1ee1c8fdb682c19dbb80ffff04ffff01a057bfd1cb0adda3d94315053fda723f2028320faa8338225d99f629e3d46d43a9ffff04ffff01ff02ffff01ff02ff0affff04ff02ffff04ff03ff80808080ffff04ffff01ffff333effff02ffff03ff05ffff01ff04ffff04ff0cffff04ffff02ff1effff04ff02ffff04ff09ff80808080ff808080ffff02ff16ffff04ff02ffff04ff19ffff04ffff02ff0affff04ff02ffff04ff0dff80808080ff808080808080ff8080ff0180ffff02ffff03ff05ffff01ff04ffff04ff08ff0980ffff02ff16ffff04ff02ffff04ff0dffff04ff0bff808080808080ffff010b80ff0180ff02ffff03ffff07ff0580ffff01ff0bffff0102ffff02ff1effff04ff02ffff04ff09ff80808080ffff02ff1effff04ff02ffff04ff0dff8080808080ffff01ff0bffff0101ff058080ff0180ff018080ff018080808080ff01808080ffffa00000000000000000000000000000000000000000000000000000000000000000ffffa00000000000000000000000000000000000000000000000000000000000000000ff01ff8080808032dbe6d545f24635c7871ea53c623c358d7cea8f5e27a983ba6e5c0bf35fa243aa064e96a86637d8f5ebe153dc8645d29f43bee762d5ec10d06c8617fa60b8c50000000000000001ff02ffff01ff02ffff01ff02ffff03ffff18ff2fff3480ffff01ff04ffff04ff20ffff04ff2fff808080ffff04ffff02ff3effff04ff02ffff04ff05ffff04ffff02ff2affff04ff02ffff04ff27ffff04ffff02ffff03ff77ffff01ff02ff36ffff04ff02ffff04ff09ffff04ff57ffff04ffff02ff2effff04ff02ffff04ff05ff80808080ff808080808080ffff011d80ff0180ffff04ffff02ffff03ff77ffff0181b7ffff015780ff0180ff808080808080ffff04ff77ff808080808080ffff02ff3affff04ff02ffff04ff05ffff04ffff02ff0bff5f80ffff01ff8080808080808080ffff01ff088080ff0180ffff04ffff01ffffffff4947ff0233ffff0401ff0102ffffff20ff02ffff03ff05ffff01ff02ff32ffff04ff02ffff04ff0dffff04ffff0bff3cffff0bff34ff2480ffff0bff3cffff0bff3cffff0bff34ff2c80ff0980ffff0bff3cff0bffff0bff34ff8080808080ff8080808080ffff010b80ff0180ffff02ffff03ffff22ffff09ffff0dff0580ff2280ffff09ffff0dff0b80ff2280ffff15ff17ffff0181ff8080ffff01ff0bff05ff0bff1780ffff01ff088080ff0180ff02ffff03ff0bffff01ff02ffff03ffff02ff26ffff04ff02ffff04ff13ff80808080ffff01ff02ffff03ffff20ff1780ffff01ff02ffff03ffff09ff81b3ffff01818f80ffff01ff02ff3affff04ff02ffff04ff05ffff04ff1bffff04ff34ff808080808080ffff01ff04ffff04ff23ffff04ffff02ff36ffff04ff02ffff04ff09ffff04ff53ffff04ffff02ff2effff04ff02ffff04ff05ff80808080ff808080808080ff738080ffff02ff3affff04ff02ffff04ff05ffff04ff1bffff04ff34ff8080808080808080ff0180ffff01ff088080ff0180ffff01ff04ff13ffff02ff3affff04ff02ffff04ff05ffff04ff1bffff04ff17ff8080808080808080ff0180ffff01ff02ffff03ff17ff80ffff01ff088080ff018080ff0180ffffff02ffff03ffff09ff09ff3880ffff01ff02ffff03ffff18ff2dffff010180ffff01ff0101ff8080ff0180ff8080ff0180ff0bff3cffff0bff34ff2880ffff0bff3cffff0bff3cffff0bff34ff2c80ff0580ffff0bff3cffff02ff32ffff04ff02ffff04ff07ffff04ffff0bff34ff3480ff8080808080ffff0bff34ff8080808080ffff02ffff03ffff07ff0580ffff01ff0bffff0102ffff02ff2effff04ff02ffff04ff09ff80808080ffff02ff2effff04ff02ffff04ff0dff8080808080ffff01ff0bffff0101ff058080ff0180ff02ffff03ffff21ff17ffff09ff0bff158080ffff01ff04ff30ffff04ff0bff808080ffff01ff088080ff0180ff018080ffff04ffff01ffa07faa3253bfddd1e0decb0906b2dc6247bbc4cf608f58345d173adb63e8b47c9fffa0a14daf55d41ced6419bcd011fbc1f74ab9567fe55340d88435aa6493d628fa47a0eff07522495060c066f66f32acc2a77e3a3e737aca8baea4d1a64ea4cdc13da9ffff04ffff01ff02ffff01ff02ffff01ff02ff3effff04ff02ffff04ff05ffff04ffff02ff2fff5f80ffff04ff80ffff04ffff04ffff04ff0bffff04ff17ff808080ffff01ff808080ffff01ff8080808080808080ffff04ffff01ffffff0233ff04ff0101ffff02ff02ffff03ff05ffff01ff02ff1affff04ff02ffff04ff0dffff04ffff0bff12ffff0bff2cff1480ffff0bff12ffff0bff12ffff0bff2cff3c80ff0980ffff0bff12ff0bffff0bff2cff8080808080ff8080808080ffff010b80ff0180ffff0bff12ffff0bff2cff1080ffff0bff12ffff0bff12ffff0bff2cff3c80ff0580ffff0bff12ffff02ff1affff04ff02ffff04ff07ffff04ffff0bff2cff2c80ff8080808080ffff0bff2cff8080808080ffff02ffff03ffff07ff0580ffff01ff0bffff0102ffff02ff2effff04ff02ffff04ff09ff80808080ffff02ff2effff04ff02ffff04ff0dff8080808080ffff01ff0bffff0101ff058080ff0180ff02ffff03ff0bffff01ff02ffff03ffff09ff23ff1880ffff01ff02ffff03ffff18ff81b3ff2c80ffff01ff02ffff03ffff20ff1780ffff01ff02ff3effff04ff02ffff04ff05ffff04ff1bffff04ff33ffff04ff2fffff04ff5fff8080808080808080ffff01ff088080ff0180ffff01ff04ff13ffff02ff3effff04ff02ffff04ff05ffff04ff1bffff04ff17ffff04ff2fffff04ff5fff80808080808080808080ff0180ffff01ff02ffff03ffff09ff23ffff0181e880ffff01ff02ff3effff04ff02ffff04ff05ffff04ff1bffff04ff17ffff04ffff02ffff03ffff22ffff09ffff02ff2effff04ff02ffff04ff53ff80808080ff82014f80ffff20ff5f8080ffff01ff02ff53ffff04ff818fffff04ff82014fffff04ff81b3ff8080808080ffff01ff088080ff0180ffff04ff2cff8080808080808080ffff01ff04ff13ffff02ff3effff04ff02ffff04ff05ffff04ff1bffff04ff17ffff04ff2fffff04ff5fff80808080808080808080ff018080ff0180ffff01ff04ffff04ff18ffff04ffff02ff16ffff04ff02ffff04ff05ffff04ff27ffff04ffff0bff2cff82014f80ffff04ffff02ff2effff04ff02ffff04ff818fff80808080ffff04ffff0bff2cff0580ff8080808080808080ff378080ff81af8080ff0180ff018080ffff04ffff01a0a04d9f57764f54a43e4030befb4d80026e870519aaa66334aef8304f5d0393c2ffff04ffff01ffa06661ea6604b491118b0f49c932c0f0de2ad815a57b54b6ec8fdbd1b408ae7e2780ffff04ffff01a057bfd1cb0adda3d94315053fda723f2028320faa8338225d99f629e3d46d43a9ffff04ffff01ff02ffff01ff02ffff01ff02ffff03ff0bffff01ff02ffff03ffff09ff05ffff1dff0bffff1effff0bff0bffff02ff06ffff04ff02ffff04ff17ff8080808080808080ffff01ff02ff17ff2f80ffff01ff088080ff0180ffff01ff04ffff04ff04ffff04ff05ffff04ffff02ff06ffff04ff02ffff04ff17ff80808080ff80808080ffff02ff17ff2f808080ff0180ffff04ffff01ff32ff02ffff03ffff07ff0580ffff01ff0bffff0102ffff02ff06ffff04ff02ffff04ff09ff80808080ffff02ff06ffff04ff02ffff04ff0dff8080808080ffff01ff0bffff0101ff058080ff0180ff018080ffff04ffff01b0a132fae32c98cbb7d8f5814c49ee3f0ba6ec2172c5e5f6900655a65cd2157a06a1c6eb89c68c8d2cdcee9506c2217978ff018080ff018080808080ff01808080ffffa0a14daf55d41ced6419bcd011fbc1f74ab9567fe55340d88435aa6493d628fa47ffa01804338c97f989c78d88716206c0f27315f3eb7d59417ab2eacee20f0a7ff60bff0180ff01ffffff80ffff02ffff01ff02ffff01ff02ffff03ff5fffff01ff02ff3affff04ff02ffff04ff0bffff04ff17ffff04ff2fffff04ff5fffff04ff81bfffff04ff82017fffff04ff8202ffffff04ffff02ff05ff8205ff80ff8080808080808080808080ffff01ff04ffff04ff10ffff01ff81ff8080ffff02ff05ff8205ff808080ff0180ffff04ffff01ffffff49ff3f02ff04ff0101ffff02ffff02ffff03ff05ffff01ff02ff2affff04ff02ffff04ff0dffff04ffff0bff12ffff0bff2cff1480ffff0bff12ffff0bff12ffff0bff2cff3c80ff0980ffff0bff12ff0bffff0bff2cff8080808080ff8080808080ffff010b80ff0180ff02ffff03ff05ffff01ff02ffff03ffff02ff3effff04ff02ffff04ff82011fffff04ff27ffff04ff4fff808080808080ffff01ff02ff3affff04ff02ffff04ff0dffff04ff1bffff04ff37ffff04ff6fffff04ff81dfffff04ff8201bfffff04ff82037fffff04ffff04ffff04ff28ffff04ffff0bffff02ff26ffff04ff02ffff04ff11ffff04ffff02ff26ffff04ff02ffff04ff13ffff04ff82027fffff04ffff02ff36ffff04ff02ffff04ff82013fff80808080ffff04ffff02ff36ffff04ff02ffff04ff819fff80808080ffff04ffff02ff36ffff04ff02ffff04ff13ff80808080ff8080808080808080ffff04ffff02ff36ffff04ff02ffff04ff09ff80808080ff808080808080ffff012480ff808080ff8202ff80ff8080808080808080808080ffff01ff088080ff0180ffff018202ff80ff0180ffffff0bff12ffff0bff2cff3880ffff0bff12ffff0bff12ffff0bff2cff3c80ff0580ffff0bff12ffff02ff2affff04ff02ffff04ff07ffff04ffff0bff2cff2c80ff8080808080ffff0bff2cff8080808080ff02ffff03ffff07ff0580ffff01ff0bffff0102ffff02ff36ffff04ff02ffff04ff09ff80808080ffff02ff36ffff04ff02ffff04ff0dff8080808080ffff01ff0bffff0101ff058080ff0180ffff02ffff03ff1bffff01ff02ff2effff04ff02ffff04ffff02ffff03ffff18ffff0101ff1380ffff01ff0bffff0102ff2bff0580ffff01ff0bffff0102ff05ff2b8080ff0180ffff04ffff04ffff17ff13ffff0181ff80ff3b80ff8080808080ffff010580ff0180ff02ffff03ff17ffff01ff02ffff03ffff09ff05ffff02ff2effff04ff02ffff04ff13ffff04ff27ff808080808080ffff01ff02ff3effff04ff02ffff04ff05ffff04ff1bffff04ff37ff808080808080ffff01ff088080ff0180ffff01ff010180ff0180ff018080ffff04ffff01ff01ffff81e8ff0bffffffffa08e54f5066aa7999fc1561a56df59d11ff01f7df93cadf49a61adebf65dec65ea80ffa057bfd1cb0adda3d94315053fda723f2028320faa8338225d99f629e3d46d43a980ff808080ffff33ffa0ca77e42ac3b3375edc54af271f21d075afd02d72969cababeec63e22f7ab10deff01ffffa0a14daf55d41ced6419bcd011fbc1f74ab9567fe55340d88435aa6493d628fa47ffa08e54f5066aa7999fc1561a56df59d11ff01f7df93cadf49a61adebf65dec65eaffa0c842b1a384b8633ac25d0f12bd7b614f86a77642ab6426418750f2b0b86bab2a8080ffff3fffa0bd7aa54c5f93ef1738439aa60b471ce2aa4c62fb18a7943aa10061f00dbdb8368080ffff04ffff01ffffa07faa3253bfddd1e0decb0906b2dc6247bbc4cf608f58345d173adb63e8b47c9fffa07acfcbd1ed73bfe2b698508f4ea5ed353c60ace154360272ce91f9ab0c8423c3a0eff07522495060c066f66f32acc2a77e3a3e737aca8baea4d1a64ea4cdc13da980ffff04ffff01ffa0a04d9f57764f54a43e4030befb4d80026e870519aaa66334aef8304f5d0393c280ffff04ffff01ffffa05743f9c9e6f3ebd1506342bbf0a6bfb9dc68b58b3e7f6f32da759fb0fb74fe0e8080ff018080808080ffff80ff80ff80ff80ff8080808080ca2e21c90d263e63b73d449a3f8d57b9458846f7af27d9a61a515395fa14071ea55bba78c76265b4bac257251b1e89dd13637a7c18e8dcb03e092dfb7eb5a84a0000000000000001ff02ffff01ff02ffff01ff02ffff03ffff18ff2fff3480ffff01ff04ffff04ff20ffff04ff2fff808080ffff04ffff02ff3effff04ff02ffff04ff05ffff04ffff02ff2affff04ff02ffff04ff27ffff04ffff02ffff03ff77ffff01ff02ff36ffff04ff02ffff04ff09ffff04ff57ffff04ffff02ff2effff04ff02ffff04ff05ff80808080ff808080808080ffff011d80ff0180ffff04ffff02ffff03ff77ffff0181b7ffff015780ff0180ff808080808080ffff04ff77ff808080808080ffff02ff3affff04ff02ffff04ff05ffff04ffff02ff0bff5f80ffff01ff8080808080808080ffff01ff088080ff0180ffff04ffff01ffffffff4947ff0233ffff0401ff0102ffffff20ff02ffff03ff05ffff01ff02ff32ffff04ff02ffff04ff0dffff04ffff0bff3cffff0bff34ff2480ffff0bff3cffff0bff3cffff0bff34ff2c80ff0980ffff0bff3cff0bffff0bff34ff8080808080ff8080808080ffff010b80ff0180ffff02ffff03ffff22ffff09ffff0dff0580ff2280ffff09ffff0dff0b80ff2280ffff15ff17ffff0181ff8080ffff01ff0bff05ff0bff1780ffff01ff088080ff0180ff02ffff03ff0bffff01ff02ffff03ffff02ff26ffff04ff02ffff04ff13ff80808080ffff01ff02ffff03ffff20ff1780ffff01ff02ffff03ffff09ff81b3ffff01818f80ffff01ff02ff3affff04ff02ffff04ff05ffff04ff1bffff04ff34ff808080808080ffff01ff04ffff04ff23ffff04ffff02ff36ffff04ff02ffff04ff09ffff04ff53ffff04ffff02ff2effff04ff02ffff04ff05ff80808080ff808080808080ff738080ffff02ff3affff04ff02ffff04ff05ffff04ff1bffff04ff34ff8080808080808080ff0180ffff01ff088080ff0180ffff01ff04ff13ffff02ff3affff04ff02ffff04ff05ffff04ff1bffff04ff17ff8080808080808080ff0180ffff01ff02ffff03ff17ff80ffff01ff088080ff018080ff0180ffffff02ffff03ffff09ff09ff3880ffff01ff02ffff03ffff18ff2dffff010180ffff01ff0101ff8080ff0180ff8080ff0180ff0bff3cffff0bff34ff2880ffff0bff3cffff0bff3cffff0bff34ff2c80ff0580ffff0bff3cffff02ff32ffff04ff02ffff04ff07ffff04ffff0bff34ff3480ff8080808080ffff0bff34ff8080808080ffff02ffff03ffff07ff0580ffff01ff0bffff0102ffff02ff2effff04ff02ffff04ff09ff80808080ffff02ff2effff04ff02ffff04ff0dff8080808080ffff01ff0bffff0101ff058080ff0180ff02ffff03ffff21ff17ffff09ff0bff158080ffff01ff04ff30ffff04ff0bff808080ffff01ff088080ff0180ff018080ffff04ffff01ffa07faa3253bfddd1e0decb0906b2dc6247bbc4cf608f58345d173adb63e8b47c9fffa0a14daf55d41ced6419bcd011fbc1f74ab9567fe55340d88435aa6493d628fa47a0eff07522495060c066f66f32acc2a77e3a3e737aca8baea4d1a64ea4cdc13da9ffff04ffff01ff02ffff01ff02ffff01ff02ff3effff04ff02ffff04ff05ffff04ffff02ff2fff5f80ffff04ff80ffff04ffff04ffff04ff0bffff04ff17ff808080ffff01ff808080ffff01ff8080808080808080ffff04ffff01ffffff0233ff04ff0101ffff02ff02ffff03ff05ffff01ff02ff1affff04ff02ffff04ff0dffff04ffff0bff12ffff0bff2cff1480ffff0bff12ffff0bff12ffff0bff2cff3c80ff0980ffff0bff12ff0bffff0bff2cff8080808080ff8080808080ffff010b80ff0180ffff0bff12ffff0bff2cff1080ffff0bff12ffff0bff12ffff0bff2cff3c80ff0580ffff0bff12ffff02ff1affff04ff02ffff04ff07ffff04ffff0bff2cff2c80ff8080808080ffff0bff2cff8080808080ffff02ffff03ffff07ff0580ffff01ff0bffff0102ffff02ff2effff04ff02ffff04ff09ff80808080ffff02ff2effff04ff02ffff04ff0dff8080808080ffff01ff0bffff0101ff058080ff0180ff02ffff03ff0bffff01ff02ffff03ffff09ff23ff1880ffff01ff02ffff03ffff18ff81b3ff2c80ffff01ff02ffff03ffff20ff1780ffff01ff02ff3effff04ff02ffff04ff05ffff04ff1bffff04ff33ffff04ff2fffff04ff5fff8080808080808080ffff01ff088080ff0180ffff01ff04ff13ffff02ff3effff04ff02ffff04ff05ffff04ff1bffff04ff17ffff04ff2fffff04ff5fff80808080808080808080ff0180ffff01ff02ffff03ffff09ff23ffff0181e880ffff01ff02ff3effff04ff02ffff04ff05ffff04ff1bffff04ff17ffff04ffff02ffff03ffff22ffff09ffff02ff2effff04ff02ffff04ff53ff80808080ff82014f80ffff20ff5f8080ffff01ff02ff53ffff04ff818fffff04ff82014fffff04ff81b3ff8080808080ffff01ff088080ff0180ffff04ff2cff8080808080808080ffff01ff04ff13ffff02ff3effff04ff02ffff04ff05ffff04ff1bffff04ff17ffff04ff2fffff04ff5fff80808080808080808080ff018080ff0180ffff01ff04ffff04ff18ffff04ffff02ff16ffff04ff02ffff04ff05ffff04ff27ffff04ffff0bff2cff82014f80ffff04ffff02ff2effff04ff02ffff04ff818fff80808080ffff04ffff0bff2cff0580ff8080808080808080ff378080ff81af8080ff0180ff018080ffff04ffff01a0a04d9f57764f54a43e4030befb4d80026e870519aaa66334aef8304f5d0393c2ffff04ffff01ffa08e54f5066aa7999fc1561a56df59d11ff01f7df93cadf49a61adebf65dec65ea80ffff04ffff01a057bfd1cb0adda3d94315053fda723f2028320faa8338225d99f629e3d46d43a9ffff04ffff01ff01ffff33ffa0c842b1a384b8633ac25d0f12bd7b614f86a77642ab6426418750f2b0b86bab2aff01ffffa0a14daf55d41ced6419bcd011fbc1f74ab9567fe55340d88435aa6493d628fa47ffa08e54f5066aa7999fc1561a56df59d11ff01f7df93cadf49a61adebf65dec65eaffa0c842b1a384b8633ac25d0f12bd7b614f86a77642ab6426418750f2b0b86bab2a8080ffff3eff248080ff018080808080ff01808080ffffa032dbe6d545f24635c7871ea53c623c358d7cea8f5e27a983ba6e5c0bf35fa243ffa08c4aebb18e8ce08405083c3d90a29f30239865142e2dcbca5393f40df9e3821dff0180ff01ffff808080820c0c567e54184c6a329f6c4614dc4af03211d1c199e9c7fe036237ba4d57b058a13897d223d9b8210aff382444747701f3fc1289dbe18ddd34dd7ca141facefbcbfd38369f556528c55458745f9981b26f30bc0710dac56d5074225e7df868",  # noqa: E501
        "taker": [
            {
                "store_id": "7acfcbd1ed73bfe2b698508f4ea5ed353c60ace154360272ce91f9ab0c8423c3",
                "inclusions": [{"key": "09", "value": "0210"}],
            }
        ],
        "maker": [
            {
                "store_id": "a14daf55d41ced6419bcd011fbc1f74ab9567fe55340d88435aa6493d628fa47",
                "proofs": [
                    {
                        "key": "10",
                        "value": "0110",
                        "node_hash": "de4ec93c032f5117d8af076dfc86faa5987a6c0b1d52ffc9cf0dfa43989d8c58",
                        "layers": [
                            {
                                "other_hash_side": "left",
                                "other_hash": "1c8ab812b97f5a9da0ba4be2380104810fe5c8022efe9b9e2c9d188fc3537434",
                                "combined_hash": "d340000b3a6717a5a8d42b24516ff69430235c771f8a527554b357b7f03c6de0",
                            },
                            {
                                "other_hash_side": "right",
                                "other_hash": "54e8b4cac761778f396840b343c0f1cb0e1fd0c9927d48d2f0d09a7a6f225126",
                                "combined_hash": "7676004a15439e4e8345d0f9f3a15500805b3447285904b7bcd7d14e27381d2e",
                            },
                            {
                                "other_hash_side": "right",
                                "other_hash": "6a37ca2d9a37a50f2d53387c3cf31395c72d75b1aacfa4402c32dc6d354542b4",
                                "combined_hash": "b1dc97f797a32631483c11d33b4759f5b498b512b7436286d1dc00bb1024b7e2",
                            },
                            {
                                "other_hash_side": "right",
                                "other_hash": "bcff6f16886339a196a2f6c842ad6d350a8579d123eb8602a0a85965ba25d671",
                                "combined_hash": "8e54f5066aa7999fc1561a56df59d11ff01f7df93cadf49a61adebf65dec65ea",
                            },
                        ],
                    }
                ],
            }
        ],
    },
    maker_inclusions=[{"key": b"\x10".hex(), "value": b"\x01\x10".hex()}],
    taker_inclusions=[{"key": b"\x09".hex(), "value": b"\x02\x10".hex()}],
    trade_id="704bb240acebbd90cca20db1b016ab5b012d9e19d901ac63dedb772d9bcf7177",
    maker_root_history=[
        bytes32.from_hexstr("6661ea6604b491118b0f49c932c0f0de2ad815a57b54b6ec8fdbd1b408ae7e27"),
        bytes32.from_hexstr("8e54f5066aa7999fc1561a56df59d11ff01f7df93cadf49a61adebf65dec65ea"),
    ],
    taker_root_history=[
        bytes32.from_hexstr("42f08ebc0578f2cec7a9ad1c3038e74e0f30eba5c2f4cb1ee1c8fdb682c19dbb"),
        bytes32.from_hexstr("d77afd64e9f307f3250a352c155480311512f9da2033228f1a2f0a3687cc90e0"),
    ],
)


make_one_take_one_unpopulated_reference = MakeAndTakeReference(
    entries_to_insert=0,
    make_offer_response={
        "trade_id": "b70e9420ff19225928b163a57bc64cf36a9c7f617f25b01b151689b690170940",
        "offer": "000000030000000000000000000000000000000000000000000000000000000000000000432efad4f24efab3b1210f81126d4bff6c8f95ff89cbee77af4962996f0456210000000000000000ff02ffff01ff02ffff01ff02ffff03ffff18ff2fff3480ffff01ff04ffff04ff20ffff04ff2fff808080ffff04ffff02ff3effff04ff02ffff04ff05ffff04ffff02ff2affff04ff02ffff04ff27ffff04ffff02ffff03ff77ffff01ff02ff36ffff04ff02ffff04ff09ffff04ff57ffff04ffff02ff2effff04ff02ffff04ff05ff80808080ff808080808080ffff011d80ff0180ffff04ffff02ffff03ff77ffff0181b7ffff015780ff0180ff808080808080ffff04ff77ff808080808080ffff02ff3affff04ff02ffff04ff05ffff04ffff02ff0bff5f80ffff01ff8080808080808080ffff01ff088080ff0180ffff04ffff01ffffffff4947ff0233ffff0401ff0102ffffff20ff02ffff03ff05ffff01ff02ff32ffff04ff02ffff04ff0dffff04ffff0bff3cffff0bff34ff2480ffff0bff3cffff0bff3cffff0bff34ff2c80ff0980ffff0bff3cff0bffff0bff34ff8080808080ff8080808080ffff010b80ff0180ffff02ffff03ffff22ffff09ffff0dff0580ff2280ffff09ffff0dff0b80ff2280ffff15ff17ffff0181ff8080ffff01ff0bff05ff0bff1780ffff01ff088080ff0180ff02ffff03ff0bffff01ff02ffff03ffff02ff26ffff04ff02ffff04ff13ff80808080ffff01ff02ffff03ffff20ff1780ffff01ff02ffff03ffff09ff81b3ffff01818f80ffff01ff02ff3affff04ff02ffff04ff05ffff04ff1bffff04ff34ff808080808080ffff01ff04ffff04ff23ffff04ffff02ff36ffff04ff02ffff04ff09ffff04ff53ffff04ffff02ff2effff04ff02ffff04ff05ff80808080ff808080808080ff738080ffff02ff3affff04ff02ffff04ff05ffff04ff1bffff04ff34ff8080808080808080ff0180ffff01ff088080ff0180ffff01ff04ff13ffff02ff3affff04ff02ffff04ff05ffff04ff1bffff04ff17ff8080808080808080ff0180ffff01ff02ffff03ff17ff80ffff01ff088080ff018080ff0180ffffff02ffff03ffff09ff09ff3880ffff01ff02ffff03ffff18ff2dffff010180ffff01ff0101ff8080ff0180ff8080ff0180ff0bff3cffff0bff34ff2880ffff0bff3cffff0bff3cffff0bff34ff2c80ff0580ffff0bff3cffff02ff32ffff04ff02ffff04ff07ffff04ffff0bff34ff3480ff8080808080ffff0bff34ff8080808080ffff02ffff03ffff07ff0580ffff01ff0bffff0102ffff02ff2effff04ff02ffff04ff09ff80808080ffff02ff2effff04ff02ffff04ff0dff8080808080ffff01ff0bffff0101ff058080ff0180ff02ffff03ffff21ff17ffff09ff0bff158080ffff01ff04ff30ffff04ff0bff808080ffff01ff088080ff0180ff018080ffff04ffff01ffa07faa3253bfddd1e0decb0906b2dc6247bbc4cf608f58345d173adb63e8b47c9fffa07acfcbd1ed73bfe2b698508f4ea5ed353c60ace154360272ce91f9ab0c8423c3a0eff07522495060c066f66f32acc2a77e3a3e737aca8baea4d1a64ea4cdc13da9ffff04ffff01ff02ffff01ff02ffff01ff02ff3effff04ff02ffff04ff05ffff04ffff02ff2fff5f80ffff04ff80ffff04ffff04ffff04ff0bffff04ff17ff808080ffff01ff808080ffff01ff8080808080808080ffff04ffff01ffffff0233ff04ff0101ffff02ff02ffff03ff05ffff01ff02ff1affff04ff02ffff04ff0dffff04ffff0bff12ffff0bff2cff1480ffff0bff12ffff0bff12ffff0bff2cff3c80ff0980ffff0bff12ff0bffff0bff2cff8080808080ff8080808080ffff010b80ff0180ffff0bff12ffff0bff2cff1080ffff0bff12ffff0bff12ffff0bff2cff3c80ff0580ffff0bff12ffff02ff1affff04ff02ffff04ff07ffff04ffff0bff2cff2c80ff8080808080ffff0bff2cff8080808080ffff02ffff03ffff07ff0580ffff01ff0bffff0102ffff02ff2effff04ff02ffff04ff09ff80808080ffff02ff2effff04ff02ffff04ff0dff8080808080ffff01ff0bffff0101ff058080ff0180ff02ffff03ff0bffff01ff02ffff03ffff09ff23ff1880ffff01ff02ffff03ffff18ff81b3ff2c80ffff01ff02ffff03ffff20ff1780ffff01ff02ff3effff04ff02ffff04ff05ffff04ff1bffff04ff33ffff04ff2fffff04ff5fff8080808080808080ffff01ff088080ff0180ffff01ff04ff13ffff02ff3effff04ff02ffff04ff05ffff04ff1bffff04ff17ffff04ff2fffff04ff5fff80808080808080808080ff0180ffff01ff02ffff03ffff09ff23ffff0181e880ffff01ff02ff3effff04ff02ffff04ff05ffff04ff1bffff04ff17ffff04ffff02ffff03ffff22ffff09ffff02ff2effff04ff02ffff04ff53ff80808080ff82014f80ffff20ff5f8080ffff01ff02ff53ffff04ff818fffff04ff82014fffff04ff81b3ff8080808080ffff01ff088080ff0180ffff04ff2cff8080808080808080ffff01ff04ff13ffff02ff3effff04ff02ffff04ff05ffff04ff1bffff04ff17ffff04ff2fffff04ff5fff80808080808080808080ff018080ff0180ffff01ff04ffff04ff18ffff04ffff02ff16ffff04ff02ffff04ff05ffff04ff27ffff04ffff0bff2cff82014f80ffff04ffff02ff2effff04ff02ffff04ff818fff80808080ffff04ffff0bff2cff0580ff8080808080808080ff378080ff81af8080ff0180ff018080ffff04ffff01a0a04d9f57764f54a43e4030befb4d80026e870519aaa66334aef8304f5d0393c2ffff04ffff01ffa0000000000000000000000000000000000000000000000000000000000000000080ffff04ffff01a057bfd1cb0adda3d94315053fda723f2028320faa8338225d99f629e3d46d43a9ffff04ffff01ff02ffff01ff02ff0affff04ff02ffff04ff03ff80808080ffff04ffff01ffff333effff02ffff03ff05ffff01ff04ffff04ff0cffff04ffff02ff1effff04ff02ffff04ff09ff80808080ff808080ffff02ff16ffff04ff02ffff04ff19ffff04ffff02ff0affff04ff02ffff04ff0dff80808080ff808080808080ff8080ff0180ffff02ffff03ff05ffff01ff04ffff04ff08ff0980ffff02ff16ffff04ff02ffff04ff0dffff04ff0bff808080808080ffff010b80ff0180ff02ffff03ffff07ff0580ffff01ff0bffff0102ffff02ff1effff04ff02ffff04ff09ff80808080ffff02ff1effff04ff02ffff04ff0dff8080808080ffff01ff0bffff0101ff058080ff0180ff018080ff018080808080ff01808080ffffa00000000000000000000000000000000000000000000000000000000000000000ffffa00000000000000000000000000000000000000000000000000000000000000000ff01ff80808080a14daf55d41ced6419bcd011fbc1f74ab9567fe55340d88435aa6493d628fa478416871446884ef363bd105960c464b4208a293b348f0f1c2e12140df38469450000000000000001ff02ffff01ff02ffff01ff02ffff03ffff18ff2fff3480ffff01ff04ffff04ff20ffff04ff2fff808080ffff04ffff02ff3effff04ff02ffff04ff05ffff04ffff02ff2affff04ff02ffff04ff27ffff04ffff02ffff03ff77ffff01ff02ff36ffff04ff02ffff04ff09ffff04ff57ffff04ffff02ff2effff04ff02ffff04ff05ff80808080ff808080808080ffff011d80ff0180ffff04ffff02ffff03ff77ffff0181b7ffff015780ff0180ff808080808080ffff04ff77ff808080808080ffff02ff3affff04ff02ffff04ff05ffff04ffff02ff0bff5f80ffff01ff8080808080808080ffff01ff088080ff0180ffff04ffff01ffffffff4947ff0233ffff0401ff0102ffffff20ff02ffff03ff05ffff01ff02ff32ffff04ff02ffff04ff0dffff04ffff0bff3cffff0bff34ff2480ffff0bff3cffff0bff3cffff0bff34ff2c80ff0980ffff0bff3cff0bffff0bff34ff8080808080ff8080808080ffff010b80ff0180ffff02ffff03ffff22ffff09ffff0dff0580ff2280ffff09ffff0dff0b80ff2280ffff15ff17ffff0181ff8080ffff01ff0bff05ff0bff1780ffff01ff088080ff0180ff02ffff03ff0bffff01ff02ffff03ffff02ff26ffff04ff02ffff04ff13ff80808080ffff01ff02ffff03ffff20ff1780ffff01ff02ffff03ffff09ff81b3ffff01818f80ffff01ff02ff3affff04ff02ffff04ff05ffff04ff1bffff04ff34ff808080808080ffff01ff04ffff04ff23ffff04ffff02ff36ffff04ff02ffff04ff09ffff04ff53ffff04ffff02ff2effff04ff02ffff04ff05ff80808080ff808080808080ff738080ffff02ff3affff04ff02ffff04ff05ffff04ff1bffff04ff34ff8080808080808080ff0180ffff01ff088080ff0180ffff01ff04ff13ffff02ff3affff04ff02ffff04ff05ffff04ff1bffff04ff17ff8080808080808080ff0180ffff01ff02ffff03ff17ff80ffff01ff088080ff018080ff0180ffffff02ffff03ffff09ff09ff3880ffff01ff02ffff03ffff18ff2dffff010180ffff01ff0101ff8080ff0180ff8080ff0180ff0bff3cffff0bff34ff2880ffff0bff3cffff0bff3cffff0bff34ff2c80ff0580ffff0bff3cffff02ff32ffff04ff02ffff04ff07ffff04ffff0bff34ff3480ff8080808080ffff0bff34ff8080808080ffff02ffff03ffff07ff0580ffff01ff0bffff0102ffff02ff2effff04ff02ffff04ff09ff80808080ffff02ff2effff04ff02ffff04ff0dff8080808080ffff01ff0bffff0101ff058080ff0180ff02ffff03ffff21ff17ffff09ff0bff158080ffff01ff04ff30ffff04ff0bff808080ffff01ff088080ff0180ff018080ffff04ffff01ffa07faa3253bfddd1e0decb0906b2dc6247bbc4cf608f58345d173adb63e8b47c9fffa0a14daf55d41ced6419bcd011fbc1f74ab9567fe55340d88435aa6493d628fa47a0eff07522495060c066f66f32acc2a77e3a3e737aca8baea4d1a64ea4cdc13da9ffff04ffff01ff02ffff01ff02ffff01ff02ff3effff04ff02ffff04ff05ffff04ffff02ff2fff5f80ffff04ff80ffff04ffff04ffff04ff0bffff04ff17ff808080ffff01ff808080ffff01ff8080808080808080ffff04ffff01ffffff0233ff04ff0101ffff02ff02ffff03ff05ffff01ff02ff1affff04ff02ffff04ff0dffff04ffff0bff12ffff0bff2cff1480ffff0bff12ffff0bff12ffff0bff2cff3c80ff0980ffff0bff12ff0bffff0bff2cff8080808080ff8080808080ffff010b80ff0180ffff0bff12ffff0bff2cff1080ffff0bff12ffff0bff12ffff0bff2cff3c80ff0580ffff0bff12ffff02ff1affff04ff02ffff04ff07ffff04ffff0bff2cff2c80ff8080808080ffff0bff2cff8080808080ffff02ffff03ffff07ff0580ffff01ff0bffff0102ffff02ff2effff04ff02ffff04ff09ff80808080ffff02ff2effff04ff02ffff04ff0dff8080808080ffff01ff0bffff0101ff058080ff0180ff02ffff03ff0bffff01ff02ffff03ffff09ff23ff1880ffff01ff02ffff03ffff18ff81b3ff2c80ffff01ff02ffff03ffff20ff1780ffff01ff02ff3effff04ff02ffff04ff05ffff04ff1bffff04ff33ffff04ff2fffff04ff5fff8080808080808080ffff01ff088080ff0180ffff01ff04ff13ffff02ff3effff04ff02ffff04ff05ffff04ff1bffff04ff17ffff04ff2fffff04ff5fff80808080808080808080ff0180ffff01ff02ffff03ffff09ff23ffff0181e880ffff01ff02ff3effff04ff02ffff04ff05ffff04ff1bffff04ff17ffff04ffff02ffff03ffff22ffff09ffff02ff2effff04ff02ffff04ff53ff80808080ff82014f80ffff20ff5f8080ffff01ff02ff53ffff04ff818fffff04ff82014fffff04ff81b3ff8080808080ffff01ff088080ff0180ffff04ff2cff8080808080808080ffff01ff04ff13ffff02ff3effff04ff02ffff04ff05ffff04ff1bffff04ff17ffff04ff2fffff04ff5fff80808080808080808080ff018080ff0180ffff01ff04ffff04ff18ffff04ffff02ff16ffff04ff02ffff04ff05ffff04ff27ffff04ffff0bff2cff82014f80ffff04ffff02ff2effff04ff02ffff04ff818fff80808080ffff04ffff0bff2cff0580ff8080808080808080ff378080ff81af8080ff0180ff018080ffff04ffff01a0a04d9f57764f54a43e4030befb4d80026e870519aaa66334aef8304f5d0393c2ffff04ffff01ffa0000000000000000000000000000000000000000000000000000000000000000080ffff04ffff01a057bfd1cb0adda3d94315053fda723f2028320faa8338225d99f629e3d46d43a9ffff04ffff01ff02ffff01ff02ffff01ff02ffff03ff0bffff01ff02ffff03ffff09ff05ffff1dff0bffff1effff0bff0bffff02ff06ffff04ff02ffff04ff17ff8080808080808080ffff01ff02ff17ff2f80ffff01ff088080ff0180ffff01ff04ffff04ff04ffff04ff05ffff04ffff02ff06ffff04ff02ffff04ff17ff80808080ff80808080ffff02ff17ff2f808080ff0180ffff04ffff01ff32ff02ffff03ffff07ff0580ffff01ff0bffff0102ffff02ff06ffff04ff02ffff04ff09ff80808080ffff02ff06ffff04ff02ffff04ff0dff8080808080ffff01ff0bffff0101ff058080ff0180ff018080ffff04ffff01b0a3b0219722055ac0a66cd9de5cd3e86962d8c8ec6abb801b57e5c77ed98453b02ceae0e19548f6d4fc20b3a2ec82aa90ff018080ff018080808080ff01808080ffffa09563629e653a9fc3c65f55947883a47e062e6b67394091228ec01352ff78f333ff0180ff01ffffff80ffff02ffff01ff02ffff01ff02ffff03ff5fffff01ff02ff3affff04ff02ffff04ff0bffff04ff17ffff04ff2fffff04ff5fffff04ff81bfffff04ff82017fffff04ff8202ffffff04ffff02ff05ff8205ff80ff8080808080808080808080ffff01ff04ffff04ff10ffff01ff81ff8080ffff02ff05ff8205ff808080ff0180ffff04ffff01ffffff49ff3f02ff04ff0101ffff02ffff02ffff03ff05ffff01ff02ff2affff04ff02ffff04ff0dffff04ffff0bff12ffff0bff2cff1480ffff0bff12ffff0bff12ffff0bff2cff3c80ff0980ffff0bff12ff0bffff0bff2cff8080808080ff8080808080ffff010b80ff0180ff02ffff03ff05ffff01ff02ffff03ffff02ff3effff04ff02ffff04ff82011fffff04ff27ffff04ff4fff808080808080ffff01ff02ff3affff04ff02ffff04ff0dffff04ff1bffff04ff37ffff04ff6fffff04ff81dfffff04ff8201bfffff04ff82037fffff04ffff04ffff04ff28ffff04ffff0bffff02ff26ffff04ff02ffff04ff11ffff04ffff02ff26ffff04ff02ffff04ff13ffff04ff82027fffff04ffff02ff36ffff04ff02ffff04ff82013fff80808080ffff04ffff02ff36ffff04ff02ffff04ff819fff80808080ffff04ffff02ff36ffff04ff02ffff04ff13ff80808080ff8080808080808080ffff04ffff02ff36ffff04ff02ffff04ff09ff80808080ff808080808080ffff012480ff808080ff8202ff80ff8080808080808080808080ffff01ff088080ff0180ffff018202ff80ff0180ffffff0bff12ffff0bff2cff3880ffff0bff12ffff0bff12ffff0bff2cff3c80ff0580ffff0bff12ffff02ff2affff04ff02ffff04ff07ffff04ffff0bff2cff2c80ff8080808080ffff0bff2cff8080808080ff02ffff03ffff07ff0580ffff01ff0bffff0102ffff02ff36ffff04ff02ffff04ff09ff80808080ffff02ff36ffff04ff02ffff04ff0dff8080808080ffff01ff0bffff0101ff058080ff0180ffff02ffff03ff1bffff01ff02ff2effff04ff02ffff04ffff02ffff03ffff18ffff0101ff1380ffff01ff0bffff0102ff2bff0580ffff01ff0bffff0102ff05ff2b8080ff0180ffff04ffff04ffff17ff13ffff0181ff80ff3b80ff8080808080ffff010580ff0180ff02ffff03ff17ffff01ff02ffff03ffff09ff05ffff02ff2effff04ff02ffff04ff13ffff04ff27ff808080808080ffff01ff02ff3effff04ff02ffff04ff05ffff04ff1bffff04ff37ff808080808080ffff01ff088080ff0180ffff01ff010180ff0180ff018080ffff04ffff01ff01ffff81e8ff0bffffffffa0de4ec93c032f5117d8af076dfc86faa5987a6c0b1d52ffc9cf0dfa43989d8c5880ffa057bfd1cb0adda3d94315053fda723f2028320faa8338225d99f629e3d46d43a980ff808080ffff33ffa0b6565d3afb87a60cfdf66bc56cca80b14afc2be649971c8df647ce617b442e6eff01ffffa0a14daf55d41ced6419bcd011fbc1f74ab9567fe55340d88435aa6493d628fa47ffa0de4ec93c032f5117d8af076dfc86faa5987a6c0b1d52ffc9cf0dfa43989d8c58ffa0846b58db3bd246785e202eeddfbb46acaf267f011307437cd4e0841f3da751f68080ffff3fffa0fccf087e5b81be2137cfaa35e65cc4e4a25183108907dad33c6d622e8e78349e8080ffff04ffff01ffffa07faa3253bfddd1e0decb0906b2dc6247bbc4cf608f58345d173adb63e8b47c9fffa07acfcbd1ed73bfe2b698508f4ea5ed353c60ace154360272ce91f9ab0c8423c3a0eff07522495060c066f66f32acc2a77e3a3e737aca8baea4d1a64ea4cdc13da980ffff04ffff01ffa0a04d9f57764f54a43e4030befb4d80026e870519aaa66334aef8304f5d0393c280ffff04ffff01ffffa07f3e180acdf046f955d3440bb3a16dfd6f5a46c809cee98e7514127327b1cab58080ff018080808080ffff80ff80ff80ff80ff808080808032dbe6d545f24635c7871ea53c623c358d7cea8f5e27a983ba6e5c0bf35fa24386ab01fbd8342f8e1dac10d6e906cef3892857bd1865b6fd7ed4b01b39d568b50000000000000001ff02ffff01ff02ffff01ff02ffff03ffff18ff2fff3480ffff01ff04ffff04ff20ffff04ff2fff808080ffff04ffff02ff3effff04ff02ffff04ff05ffff04ffff02ff2affff04ff02ffff04ff27ffff04ffff02ffff03ff77ffff01ff02ff36ffff04ff02ffff04ff09ffff04ff57ffff04ffff02ff2effff04ff02ffff04ff05ff80808080ff808080808080ffff011d80ff0180ffff04ffff02ffff03ff77ffff0181b7ffff015780ff0180ff808080808080ffff04ff77ff808080808080ffff02ff3affff04ff02ffff04ff05ffff04ffff02ff0bff5f80ffff01ff8080808080808080ffff01ff088080ff0180ffff04ffff01ffffffff4947ff0233ffff0401ff0102ffffff20ff02ffff03ff05ffff01ff02ff32ffff04ff02ffff04ff0dffff04ffff0bff3cffff0bff34ff2480ffff0bff3cffff0bff3cffff0bff34ff2c80ff0980ffff0bff3cff0bffff0bff34ff8080808080ff8080808080ffff010b80ff0180ffff02ffff03ffff22ffff09ffff0dff0580ff2280ffff09ffff0dff0b80ff2280ffff15ff17ffff0181ff8080ffff01ff0bff05ff0bff1780ffff01ff088080ff0180ff02ffff03ff0bffff01ff02ffff03ffff02ff26ffff04ff02ffff04ff13ff80808080ffff01ff02ffff03ffff20ff1780ffff01ff02ffff03ffff09ff81b3ffff01818f80ffff01ff02ff3affff04ff02ffff04ff05ffff04ff1bffff04ff34ff808080808080ffff01ff04ffff04ff23ffff04ffff02ff36ffff04ff02ffff04ff09ffff04ff53ffff04ffff02ff2effff04ff02ffff04ff05ff80808080ff808080808080ff738080ffff02ff3affff04ff02ffff04ff05ffff04ff1bffff04ff34ff8080808080808080ff0180ffff01ff088080ff0180ffff01ff04ff13ffff02ff3affff04ff02ffff04ff05ffff04ff1bffff04ff17ff8080808080808080ff0180ffff01ff02ffff03ff17ff80ffff01ff088080ff018080ff0180ffffff02ffff03ffff09ff09ff3880ffff01ff02ffff03ffff18ff2dffff010180ffff01ff0101ff8080ff0180ff8080ff0180ff0bff3cffff0bff34ff2880ffff0bff3cffff0bff3cffff0bff34ff2c80ff0580ffff0bff3cffff02ff32ffff04ff02ffff04ff07ffff04ffff0bff34ff3480ff8080808080ffff0bff34ff8080808080ffff02ffff03ffff07ff0580ffff01ff0bffff0102ffff02ff2effff04ff02ffff04ff09ff80808080ffff02ff2effff04ff02ffff04ff0dff8080808080ffff01ff0bffff0101ff058080ff0180ff02ffff03ffff21ff17ffff09ff0bff158080ffff01ff04ff30ffff04ff0bff808080ffff01ff088080ff0180ff018080ffff04ffff01ffa07faa3253bfddd1e0decb0906b2dc6247bbc4cf608f58345d173adb63e8b47c9fffa0a14daf55d41ced6419bcd011fbc1f74ab9567fe55340d88435aa6493d628fa47a0eff07522495060c066f66f32acc2a77e3a3e737aca8baea4d1a64ea4cdc13da9ffff04ffff01ff02ffff01ff02ffff01ff02ff3effff04ff02ffff04ff05ffff04ffff02ff2fff5f80ffff04ff80ffff04ffff04ffff04ff0bffff04ff17ff808080ffff01ff808080ffff01ff8080808080808080ffff04ffff01ffffff0233ff04ff0101ffff02ff02ffff03ff05ffff01ff02ff1affff04ff02ffff04ff0dffff04ffff0bff12ffff0bff2cff1480ffff0bff12ffff0bff12ffff0bff2cff3c80ff0980ffff0bff12ff0bffff0bff2cff8080808080ff8080808080ffff010b80ff0180ffff0bff12ffff0bff2cff1080ffff0bff12ffff0bff12ffff0bff2cff3c80ff0580ffff0bff12ffff02ff1affff04ff02ffff04ff07ffff04ffff0bff2cff2c80ff8080808080ffff0bff2cff8080808080ffff02ffff03ffff07ff0580ffff01ff0bffff0102ffff02ff2effff04ff02ffff04ff09ff80808080ffff02ff2effff04ff02ffff04ff0dff8080808080ffff01ff0bffff0101ff058080ff0180ff02ffff03ff0bffff01ff02ffff03ffff09ff23ff1880ffff01ff02ffff03ffff18ff81b3ff2c80ffff01ff02ffff03ffff20ff1780ffff01ff02ff3effff04ff02ffff04ff05ffff04ff1bffff04ff33ffff04ff2fffff04ff5fff8080808080808080ffff01ff088080ff0180ffff01ff04ff13ffff02ff3effff04ff02ffff04ff05ffff04ff1bffff04ff17ffff04ff2fffff04ff5fff80808080808080808080ff0180ffff01ff02ffff03ffff09ff23ffff0181e880ffff01ff02ff3effff04ff02ffff04ff05ffff04ff1bffff04ff17ffff04ffff02ffff03ffff22ffff09ffff02ff2effff04ff02ffff04ff53ff80808080ff82014f80ffff20ff5f8080ffff01ff02ff53ffff04ff818fffff04ff82014fffff04ff81b3ff8080808080ffff01ff088080ff0180ffff04ff2cff8080808080808080ffff01ff04ff13ffff02ff3effff04ff02ffff04ff05ffff04ff1bffff04ff17ffff04ff2fffff04ff5fff80808080808080808080ff018080ff0180ffff01ff04ffff04ff18ffff04ffff02ff16ffff04ff02ffff04ff05ffff04ff27ffff04ffff0bff2cff82014f80ffff04ffff02ff2effff04ff02ffff04ff818fff80808080ffff04ffff0bff2cff0580ff8080808080808080ff378080ff81af8080ff0180ff018080ffff04ffff01a0a04d9f57764f54a43e4030befb4d80026e870519aaa66334aef8304f5d0393c2ffff04ffff01ffa0de4ec93c032f5117d8af076dfc86faa5987a6c0b1d52ffc9cf0dfa43989d8c5880ffff04ffff01a057bfd1cb0adda3d94315053fda723f2028320faa8338225d99f629e3d46d43a9ffff04ffff01ff01ffff33ffa0846b58db3bd246785e202eeddfbb46acaf267f011307437cd4e0841f3da751f6ff01ffffa0a14daf55d41ced6419bcd011fbc1f74ab9567fe55340d88435aa6493d628fa47ffa0de4ec93c032f5117d8af076dfc86faa5987a6c0b1d52ffc9cf0dfa43989d8c58ffa0846b58db3bd246785e202eeddfbb46acaf267f011307437cd4e0841f3da751f68080ffff3eff248080ff018080808080ff01808080ffffa0a14daf55d41ced6419bcd011fbc1f74ab9567fe55340d88435aa6493d628fa47ffa01804338c97f989c78d88716206c0f27315f3eb7d59417ab2eacee20f0a7ff60bff0180ff01ffff808080aa82d1ac63f26fa2a0bbbe0a8ce8852e2af8381c29f62766ca685146b57669fc3036058c97588d9f74506f1d5d45e52e0d632898102837474a736a6d2140b266990d569bf71a1fcc20d4e7d4e9205b115e5820b7c4b001ff00fa833f6a299956",  # noqa: E501
        "taker": [
            {
                "store_id": "7acfcbd1ed73bfe2b698508f4ea5ed353c60ace154360272ce91f9ab0c8423c3",
                "inclusions": [{"key": "10", "value": "0210"}],
            }
        ],
        "maker": [
            {
                "store_id": "a14daf55d41ced6419bcd011fbc1f74ab9567fe55340d88435aa6493d628fa47",
                "proofs": [
                    {
                        "key": "10",
                        "value": "0110",
                        "node_hash": "de4ec93c032f5117d8af076dfc86faa5987a6c0b1d52ffc9cf0dfa43989d8c58",
                        "layers": [],
                    }
                ],
            }
        ],
    },
    maker_inclusions=[{"key": b"\x10".hex(), "value": b"\x01\x10".hex()}],
    taker_inclusions=[{"key": b"\x10".hex(), "value": b"\x02\x10".hex()}],
    trade_id="317317bb70143fcab63a945bd5f845e2b8470dc293de7f5a5c3e88bb3b4e6c31",
    maker_root_history=[bytes32.from_hexstr("de4ec93c032f5117d8af076dfc86faa5987a6c0b1d52ffc9cf0dfa43989d8c58")],
    taker_root_history=[bytes32.from_hexstr("7f3e180acdf046f955d3440bb3a16dfd6f5a46c809cee98e7514127327b1cab5")],
)


@pytest.mark.parametrize(
    argnames="reference",
    argvalues=[
        pytest.param(make_one_take_one_reference, id="one for one"),
        pytest.param(make_two_take_one_reference, id="two for one"),
        pytest.param(make_one_take_two_reference, id="one for two"),
        pytest.param(make_one_existing_take_one_reference, id="one existing for one"),
        pytest.param(make_one_take_one_existing_reference, id="one for one existing"),
        pytest.param(make_one_upsert_take_one_reference, id="one upsert for one"),
        pytest.param(make_one_take_one_upsert_reference, id="one for one upsert"),
        pytest.param(make_one_take_one_unpopulated_reference, id="one for one unpopulated"),
    ],
)
@pytest.mark.asyncio
async def test_make_and_take_offer(offer_setup: OfferSetup, reference: MakeAndTakeReference) -> None:
    offer_setup = await populate_offer_setup(offer_setup=offer_setup, count=reference.entries_to_insert)

    maker_request = {
        "maker": [
            {
                "store_id": offer_setup.maker.id.hex(),
                "inclusions": reference.maker_inclusions,
            }
        ],
        "taker": [
            {
                "store_id": offer_setup.taker.id.hex(),
                "inclusions": reference.taker_inclusions,
            }
        ],
        "fee": 0,
    }
    maker_response = await offer_setup.maker.api.make_offer(request=maker_request)
    print(f"\nmaybe_reference_offer = {maker_response['offer']}")

    assert maker_response == {"success": True, "offer": reference.make_offer_response}

    taker_request = {
        "offer": reference.make_offer_response,
        "fee": 0,
    }
    taker_response = await offer_setup.taker.api.take_offer(request=taker_request)

    assert taker_response == {
        "success": True,
        "trade_id": reference.trade_id,
    }

    await process_for_data_layer_keys(
        expected_key=hexstr_to_bytes(reference.maker_inclusions[0]["key"]),
        full_node_api=offer_setup.full_node_api,
        data_layer=offer_setup.maker.data_layer,
        store_id=offer_setup.maker.id,
    )
    await process_for_data_layer_keys(
        expected_key=hexstr_to_bytes(reference.taker_inclusions[0]["key"]),
        full_node_api=offer_setup.full_node_api,
        data_layer=offer_setup.taker.data_layer,
        store_id=offer_setup.taker.id,
    )

    maker_history_result = await offer_setup.maker.api.get_root_history(request={"id": offer_setup.maker.id.hex()})
    maker_history = maker_history_result["root_history"]
    taker_history_result = await offer_setup.taker.api.get_root_history(request={"id": offer_setup.taker.id.hex()})
    taker_history = taker_history_result["root_history"]

    assert [generation["confirmed"] for generation in maker_history] == [True] * len(maker_history)
    assert [generation["root_hash"] for generation in maker_history] == [
        bytes32([0] * 32),
        *reference.maker_root_history,
    ]

    assert [generation["confirmed"] for generation in taker_history] == [True] * len(taker_history)
    assert [generation["root_hash"] for generation in taker_history] == [
        bytes32([0] * 32),
        *reference.taker_root_history,
    ]

    # TODO: test maker and taker fees


@pytest.mark.parametrize(
    argnames="reference",
    argvalues=[
        pytest.param(make_one_take_one_reference, id="one for one"),
        pytest.param(make_two_take_one_reference, id="two for one"),
        pytest.param(make_one_take_two_reference, id="one for two"),
        pytest.param(make_one_existing_take_one_reference, id="one existing for one"),
        pytest.param(make_one_take_one_existing_reference, id="one for one existing"),
        pytest.param(make_one_upsert_take_one_reference, id="one upsert for one"),
        pytest.param(make_one_take_one_upsert_reference, id="one for one upsert"),
    ],
)
@pytest.mark.parametrize(argnames="maker_or_taker", argvalues=["maker", "taker"])
@pytest.mark.asyncio
async def test_make_and_then_take_offer_invalid_inclusion_key(
    reference: MakeAndTakeReference,
    maker_or_taker: str,
) -> None:
    broken_taker_offer = copy.deepcopy(reference.make_offer_response)
    if maker_or_taker == "maker":
        broken_taker_offer["maker"][0]["proofs"][0]["key"] += "ab"
    elif maker_or_taker == "taker":
        broken_taker_offer["taker"][0]["inclusions"][0]["key"] += "ab"
    else:
        raise Exception("invalid maker or taker choice")

    offer_bytes = hexstr_to_bytes(broken_taker_offer["offer"])
    trading_offer = TradingOffer.from_bytes(offer_bytes)

    # TODO: specific exceptions
    with pytest.raises(OfferIntegrityError):
        verify_offer(
            maker=tuple(StoreProofs.unmarshal(proof) for proof in broken_taker_offer["maker"]),
            taker=tuple(OfferStore.unmarshal(offer_store) for offer_store in broken_taker_offer["taker"]),
            summary=await DataLayerWallet.get_offer_summary(offer=trading_offer),
        )


@pytest.mark.asyncio
async def test_verify_offer_rpc_valid(bare_data_layer_api: DataLayerRpcApi) -> None:
    reference = make_one_take_one_reference

    verify_request = {
        "offer": reference.make_offer_response,
        "fee": 0,
    }
    verify_response = await bare_data_layer_api.verify_offer(request=verify_request)

    assert verify_response == {
        "success": True,
        "valid": True,
        "error": None,
        "fee": 0,
    }


@pytest.mark.asyncio
async def test_verify_offer_rpc_invalid(bare_data_layer_api: DataLayerRpcApi) -> None:
    reference = make_one_take_one_reference
    broken_taker_offer = copy.deepcopy(reference.make_offer_response)
    broken_taker_offer["maker"][0]["proofs"][0]["key"] += "ab"

    verify_request = {
        "offer": broken_taker_offer,
        "fee": 0,
    }
    verify_response = await bare_data_layer_api.verify_offer(request=verify_request)

    assert verify_response == {
        "success": True,
        "valid": False,
        "error": "maker: node hash does not match key and value",
        "fee": None,
    }


@pytest.mark.asyncio
async def test_make_offer_failure_rolls_back_db(offer_setup: OfferSetup) -> None:
    # TODO: only needs the maker and db?  wallet?
    reference = make_one_take_one_reference
    offer_setup = await populate_offer_setup(offer_setup=offer_setup, count=reference.entries_to_insert)

    maker_request = {
        "maker": [
            {
                "store_id": offer_setup.maker.id.hex(),
                "inclusions": reference.maker_inclusions,
            },
            {
                "store_id": bytes32([0] * 32).hex(),
                "inclusions": [],
            },
        ],
        "taker": [],
        "fee": 0,
    }

    with pytest.raises(Exception, match="store id not available"):
        await offer_setup.maker.api.make_offer(request=maker_request)

    pending_root = await offer_setup.maker.data_layer.data_store.get_pending_root(tree_id=offer_setup.maker.id)
    assert pending_root is None


@pytest.mark.parametrize(
    argnames="reference",
    argvalues=[
        pytest.param(make_one_take_one_reference, id="one for one"),
        pytest.param(make_two_take_one_reference, id="two for one"),
        pytest.param(make_one_take_two_reference, id="one for two"),
        pytest.param(make_one_existing_take_one_reference, id="one existing for one"),
        pytest.param(make_one_take_one_existing_reference, id="one for one existing"),
        pytest.param(make_one_upsert_take_one_reference, id="one upsert for one"),
        pytest.param(make_one_take_one_upsert_reference, id="one for one upsert"),
        pytest.param(make_one_take_one_unpopulated_reference, id="one for one unpopulated"),
    ],
)
@pytest.mark.asyncio
async def test_make_and_cancel_offer(offer_setup: OfferSetup, reference: MakeAndTakeReference) -> None:
    offer_setup = await populate_offer_setup(offer_setup=offer_setup, count=reference.entries_to_insert)

    maker_request = {
        "maker": [
            {
                "store_id": offer_setup.maker.id.hex(),
                "inclusions": reference.maker_inclusions,
            }
        ],
        "taker": [
            {
                "store_id": offer_setup.taker.id.hex(),
                "inclusions": reference.taker_inclusions,
            }
        ],
        "fee": 0,
    }
    maker_response = await offer_setup.maker.api.make_offer(request=maker_request)
    print(f"\nmaybe_reference_offer = {maker_response['offer']}")

    assert maker_response == {"success": True, "offer": reference.make_offer_response}

    cancel_request = {
        "trade_id": reference.make_offer_response["trade_id"],
        "secure": True,
        "fee": None,
    }
    await offer_setup.maker.api.cancel_offer(request=cancel_request)

    for _ in range(10):
        if not await offer_setup.maker.data_layer.wallet_rpc.check_offer_validity(
            offer=TradingOffer.from_bytes(hexstr_to_bytes(reference.make_offer_response["offer"])),
        ):
            break
        await offer_setup.full_node_api.process_blocks(count=1)
        await asyncio.sleep(0.5)
    else:
        assert False, "offer was not cancelled"

    taker_request = {
        "offer": reference.make_offer_response,
        "fee": 0,
    }

    with pytest.raises(ValueError, match="This offer is no longer valid"):
        await offer_setup.taker.api.take_offer(request=taker_request)


@pytest.mark.parametrize(
    argnames="reference",
    argvalues=[
        pytest.param(make_one_take_one_reference, id="one for one"),
        pytest.param(make_two_take_one_reference, id="two for one"),
        pytest.param(make_one_take_two_reference, id="one for two"),
        pytest.param(make_one_take_one_existing_reference, id="one for one existing"),
        pytest.param(make_one_upsert_take_one_reference, id="one upsert for one"),
        pytest.param(make_one_take_one_upsert_reference, id="one for one upsert"),
        pytest.param(make_one_take_one_unpopulated_reference, id="one for one unpopulated"),
    ],
)
@pytest.mark.asyncio
async def test_make_and_cancel_offer_not_secure_clears_pending_roots(
    offer_setup: OfferSetup,
    reference: MakeAndTakeReference,
) -> None:
    offer_setup = await populate_offer_setup(offer_setup=offer_setup, count=reference.entries_to_insert)

    maker_request = {
        "maker": [
            {
                "store_id": offer_setup.maker.id.hex(),
                "inclusions": reference.maker_inclusions,
            }
        ],
        "taker": [
            {
                "store_id": offer_setup.taker.id.hex(),
                "inclusions": reference.taker_inclusions,
            }
        ],
        "fee": 0,
    }
    maker_response = await offer_setup.maker.api.make_offer(request=maker_request)
    print(f"\nmaybe_reference_offer = {maker_response['offer']}")

    assert maker_response == {"success": True, "offer": reference.make_offer_response}

    cancel_request = {
        "trade_id": reference.make_offer_response["trade_id"],
        "secure": False,
        "fee": None,
    }
    await offer_setup.maker.api.cancel_offer(request=cancel_request)

    # make sure there is no left over pending root by inserting and publishing
    await offer_setup.maker.api.insert(request={"id": offer_setup.maker.id.hex(), "key": "ab", "value": "cd"})