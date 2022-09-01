# this file is generated by build_network_protocol_files.py

from typing import Tuple
from pathlib import Path
from tests.util.network_protocol_data import *  # noqa: F403
from tests.util.protocol_messages_json import *  # noqa: F403
from tests.util.build_network_protocol_files import get_network_protocol_filename


def parse_blob(input_bytes: bytes) -> Tuple[bytes, bytes]:
    size_bytes = input_bytes[:4]
    input_bytes = input_bytes[4:]
    size = int.from_bytes(size_bytes, "big")
    message_bytes = input_bytes[:size]
    input_bytes = input_bytes[size:]
    return (message_bytes, input_bytes)


def test_protocol_bytes() -> None:

    filename: Path = get_network_protocol_filename()
    assert filename.exists()
    with open(filename, "rb") as f:
        input_bytes = f.read()

    message_bytes, input_bytes = parse_blob(input_bytes)
    message = type(new_signage_point).from_bytes(message_bytes)
    assert message == new_signage_point
    assert bytes(message) == bytes(new_signage_point)

    message_bytes, input_bytes = parse_blob(input_bytes)
    message = type(declare_proof_of_space).from_bytes(message_bytes)
    assert message == declare_proof_of_space
    assert bytes(message) == bytes(declare_proof_of_space)

    message_bytes, input_bytes = parse_blob(input_bytes)
    message = type(request_signed_values).from_bytes(message_bytes)
    assert message == request_signed_values
    assert bytes(message) == bytes(request_signed_values)

    message_bytes, input_bytes = parse_blob(input_bytes)
    message = type(farming_info).from_bytes(message_bytes)
    assert message == farming_info
    assert bytes(message) == bytes(farming_info)

    message_bytes, input_bytes = parse_blob(input_bytes)
    message = type(signed_values).from_bytes(message_bytes)
    assert message == signed_values
    assert bytes(message) == bytes(signed_values)

    message_bytes, input_bytes = parse_blob(input_bytes)
    message = type(new_peak).from_bytes(message_bytes)
    assert message == new_peak
    assert bytes(message) == bytes(new_peak)

    message_bytes, input_bytes = parse_blob(input_bytes)
    message = type(new_transaction).from_bytes(message_bytes)
    assert message == new_transaction
    assert bytes(message) == bytes(new_transaction)

    message_bytes, input_bytes = parse_blob(input_bytes)
    message = type(request_transaction).from_bytes(message_bytes)
    assert message == request_transaction
    assert bytes(message) == bytes(request_transaction)

    message_bytes, input_bytes = parse_blob(input_bytes)
    message = type(respond_transaction).from_bytes(message_bytes)
    assert message == respond_transaction
    assert bytes(message) == bytes(respond_transaction)

    message_bytes, input_bytes = parse_blob(input_bytes)
    message = type(request_proof_of_weight).from_bytes(message_bytes)
    assert message == request_proof_of_weight
    assert bytes(message) == bytes(request_proof_of_weight)

    message_bytes, input_bytes = parse_blob(input_bytes)
    message = type(respond_proof_of_weight).from_bytes(message_bytes)
    assert message == respond_proof_of_weight
    assert bytes(message) == bytes(respond_proof_of_weight)

    message_bytes, input_bytes = parse_blob(input_bytes)
    message = type(request_block).from_bytes(message_bytes)
    assert message == request_block
    assert bytes(message) == bytes(request_block)

    message_bytes, input_bytes = parse_blob(input_bytes)
    message = type(reject_block).from_bytes(message_bytes)
    assert message == reject_block
    assert bytes(message) == bytes(reject_block)

    message_bytes, input_bytes = parse_blob(input_bytes)
    message = type(request_blocks).from_bytes(message_bytes)
    assert message == request_blocks
    assert bytes(message) == bytes(request_blocks)

    message_bytes, input_bytes = parse_blob(input_bytes)
    message = type(respond_blocks).from_bytes(message_bytes)
    assert message == respond_blocks
    assert bytes(message) == bytes(respond_blocks)

    message_bytes, input_bytes = parse_blob(input_bytes)
    message = type(reject_blocks).from_bytes(message_bytes)
    assert message == reject_blocks
    assert bytes(message) == bytes(reject_blocks)

    message_bytes, input_bytes = parse_blob(input_bytes)
    message = type(respond_block).from_bytes(message_bytes)
    assert message == respond_block
    assert bytes(message) == bytes(respond_block)

    message_bytes, input_bytes = parse_blob(input_bytes)
    message = type(new_unfinished_block).from_bytes(message_bytes)
    assert message == new_unfinished_block
    assert bytes(message) == bytes(new_unfinished_block)

    message_bytes, input_bytes = parse_blob(input_bytes)
    message = type(request_unfinished_block).from_bytes(message_bytes)
    assert message == request_unfinished_block
    assert bytes(message) == bytes(request_unfinished_block)

    message_bytes, input_bytes = parse_blob(input_bytes)
    message = type(respond_unfinished_block).from_bytes(message_bytes)
    assert message == respond_unfinished_block
    assert bytes(message) == bytes(respond_unfinished_block)

    message_bytes, input_bytes = parse_blob(input_bytes)
    message = type(new_signage_point_or_end_of_subslot).from_bytes(message_bytes)
    assert message == new_signage_point_or_end_of_subslot
    assert bytes(message) == bytes(new_signage_point_or_end_of_subslot)

    message_bytes, input_bytes = parse_blob(input_bytes)
    message = type(request_signage_point_or_end_of_subslot).from_bytes(message_bytes)
    assert message == request_signage_point_or_end_of_subslot
    assert bytes(message) == bytes(request_signage_point_or_end_of_subslot)

    message_bytes, input_bytes = parse_blob(input_bytes)
    message = type(respond_signage_point).from_bytes(message_bytes)
    assert message == respond_signage_point
    assert bytes(message) == bytes(respond_signage_point)

    message_bytes, input_bytes = parse_blob(input_bytes)
    message = type(respond_end_of_subslot).from_bytes(message_bytes)
    assert message == respond_end_of_subslot
    assert bytes(message) == bytes(respond_end_of_subslot)

    message_bytes, input_bytes = parse_blob(input_bytes)
    message = type(request_mempool_transaction).from_bytes(message_bytes)
    assert message == request_mempool_transaction
    assert bytes(message) == bytes(request_mempool_transaction)

    message_bytes, input_bytes = parse_blob(input_bytes)
    message = type(new_compact_vdf).from_bytes(message_bytes)
    assert message == new_compact_vdf
    assert bytes(message) == bytes(new_compact_vdf)

    message_bytes, input_bytes = parse_blob(input_bytes)
    message = type(request_compact_vdf).from_bytes(message_bytes)
    assert message == request_compact_vdf
    assert bytes(message) == bytes(request_compact_vdf)

    message_bytes, input_bytes = parse_blob(input_bytes)
    message = type(respond_compact_vdf).from_bytes(message_bytes)
    assert message == respond_compact_vdf
    assert bytes(message) == bytes(respond_compact_vdf)

    message_bytes, input_bytes = parse_blob(input_bytes)
    message = type(request_peers).from_bytes(message_bytes)
    assert message == request_peers
    assert bytes(message) == bytes(request_peers)

    message_bytes, input_bytes = parse_blob(input_bytes)
    message = type(respond_peers).from_bytes(message_bytes)
    assert message == respond_peers
    assert bytes(message) == bytes(respond_peers)

    message_bytes, input_bytes = parse_blob(input_bytes)
    message = type(request_puzzle_solution).from_bytes(message_bytes)
    assert message == request_puzzle_solution
    assert bytes(message) == bytes(request_puzzle_solution)

    message_bytes, input_bytes = parse_blob(input_bytes)
    message = type(puzzle_solution_response).from_bytes(message_bytes)
    assert message == puzzle_solution_response
    assert bytes(message) == bytes(puzzle_solution_response)

    message_bytes, input_bytes = parse_blob(input_bytes)
    message = type(respond_puzzle_solution).from_bytes(message_bytes)
    assert message == respond_puzzle_solution
    assert bytes(message) == bytes(respond_puzzle_solution)

    message_bytes, input_bytes = parse_blob(input_bytes)
    message = type(reject_puzzle_solution).from_bytes(message_bytes)
    assert message == reject_puzzle_solution
    assert bytes(message) == bytes(reject_puzzle_solution)

    message_bytes, input_bytes = parse_blob(input_bytes)
    message = type(send_transaction).from_bytes(message_bytes)
    assert message == send_transaction
    assert bytes(message) == bytes(send_transaction)

    message_bytes, input_bytes = parse_blob(input_bytes)
    message = type(transaction_ack).from_bytes(message_bytes)
    assert message == transaction_ack
    assert bytes(message) == bytes(transaction_ack)

    message_bytes, input_bytes = parse_blob(input_bytes)
    message = type(new_peak_wallet).from_bytes(message_bytes)
    assert message == new_peak_wallet
    assert bytes(message) == bytes(new_peak_wallet)

    message_bytes, input_bytes = parse_blob(input_bytes)
    message = type(request_block_header).from_bytes(message_bytes)
    assert message == request_block_header
    assert bytes(message) == bytes(request_block_header)

    message_bytes, input_bytes = parse_blob(input_bytes)
    message = type(request_block_headers).from_bytes(message_bytes)
    assert message == request_block_headers
    assert bytes(message) == bytes(request_block_headers)

    message_bytes, input_bytes = parse_blob(input_bytes)
    message = type(respond_header_block).from_bytes(message_bytes)
    assert message == respond_header_block
    assert bytes(message) == bytes(respond_header_block)

    message_bytes, input_bytes = parse_blob(input_bytes)
    message = type(respond_block_headers).from_bytes(message_bytes)
    assert message == respond_block_headers
    assert bytes(message) == bytes(respond_block_headers)

    message_bytes, input_bytes = parse_blob(input_bytes)
    message = type(reject_header_request).from_bytes(message_bytes)
    assert message == reject_header_request
    assert bytes(message) == bytes(reject_header_request)

    message_bytes, input_bytes = parse_blob(input_bytes)
    message = type(request_removals).from_bytes(message_bytes)
    assert message == request_removals
    assert bytes(message) == bytes(request_removals)

    message_bytes, input_bytes = parse_blob(input_bytes)
    message = type(respond_removals).from_bytes(message_bytes)
    assert message == respond_removals
    assert bytes(message) == bytes(respond_removals)

    message_bytes, input_bytes = parse_blob(input_bytes)
    message = type(reject_removals_request).from_bytes(message_bytes)
    assert message == reject_removals_request
    assert bytes(message) == bytes(reject_removals_request)

    message_bytes, input_bytes = parse_blob(input_bytes)
    message = type(request_additions).from_bytes(message_bytes)
    assert message == request_additions
    assert bytes(message) == bytes(request_additions)

    message_bytes, input_bytes = parse_blob(input_bytes)
    message = type(respond_additions).from_bytes(message_bytes)
    assert message == respond_additions
    assert bytes(message) == bytes(respond_additions)

    message_bytes, input_bytes = parse_blob(input_bytes)
    message = type(reject_additions).from_bytes(message_bytes)
    assert message == reject_additions
    assert bytes(message) == bytes(reject_additions)

    message_bytes, input_bytes = parse_blob(input_bytes)
    message = type(request_header_blocks).from_bytes(message_bytes)
    assert message == request_header_blocks
    assert bytes(message) == bytes(request_header_blocks)

    message_bytes, input_bytes = parse_blob(input_bytes)
    message = type(reject_header_blocks).from_bytes(message_bytes)
    assert message == reject_header_blocks
    assert bytes(message) == bytes(reject_header_blocks)

    message_bytes, input_bytes = parse_blob(input_bytes)
    message = type(respond_header_blocks).from_bytes(message_bytes)
    assert message == respond_header_blocks
    assert bytes(message) == bytes(respond_header_blocks)

    message_bytes, input_bytes = parse_blob(input_bytes)
    message = type(coin_state).from_bytes(message_bytes)
    assert message == coin_state
    assert bytes(message) == bytes(coin_state)

    message_bytes, input_bytes = parse_blob(input_bytes)
    message = type(register_for_ph_updates).from_bytes(message_bytes)
    assert message == register_for_ph_updates
    assert bytes(message) == bytes(register_for_ph_updates)

    message_bytes, input_bytes = parse_blob(input_bytes)
    message = type(reject_block_headers).from_bytes(message_bytes)
    assert message == reject_block_headers
    assert bytes(message) == bytes(reject_block_headers)

    message_bytes, input_bytes = parse_blob(input_bytes)
    message = type(respond_to_ph_updates).from_bytes(message_bytes)
    assert message == respond_to_ph_updates
    assert bytes(message) == bytes(respond_to_ph_updates)

    message_bytes, input_bytes = parse_blob(input_bytes)
    message = type(register_for_coin_updates).from_bytes(message_bytes)
    assert message == register_for_coin_updates
    assert bytes(message) == bytes(register_for_coin_updates)

    message_bytes, input_bytes = parse_blob(input_bytes)
    message = type(respond_to_coin_updates).from_bytes(message_bytes)
    assert message == respond_to_coin_updates
    assert bytes(message) == bytes(respond_to_coin_updates)

    message_bytes, input_bytes = parse_blob(input_bytes)
    message = type(coin_state_update).from_bytes(message_bytes)
    assert message == coin_state_update
    assert bytes(message) == bytes(coin_state_update)

    message_bytes, input_bytes = parse_blob(input_bytes)
    message = type(request_children).from_bytes(message_bytes)
    assert message == request_children
    assert bytes(message) == bytes(request_children)

    message_bytes, input_bytes = parse_blob(input_bytes)
    message = type(respond_children).from_bytes(message_bytes)
    assert message == respond_children
    assert bytes(message) == bytes(respond_children)

    message_bytes, input_bytes = parse_blob(input_bytes)
    message = type(request_ses_info).from_bytes(message_bytes)
    assert message == request_ses_info
    assert bytes(message) == bytes(request_ses_info)

    message_bytes, input_bytes = parse_blob(input_bytes)
    message = type(respond_ses_info).from_bytes(message_bytes)
    assert message == respond_ses_info
    assert bytes(message) == bytes(respond_ses_info)

    message_bytes, input_bytes = parse_blob(input_bytes)
    message = type(pool_difficulty).from_bytes(message_bytes)
    assert message == pool_difficulty
    assert bytes(message) == bytes(pool_difficulty)

    message_bytes, input_bytes = parse_blob(input_bytes)
    message = type(harvester_handhsake).from_bytes(message_bytes)
    assert message == harvester_handhsake
    assert bytes(message) == bytes(harvester_handhsake)

    message_bytes, input_bytes = parse_blob(input_bytes)
    message = type(new_signage_point_harvester).from_bytes(message_bytes)
    assert message == new_signage_point_harvester
    assert bytes(message) == bytes(new_signage_point_harvester)

    message_bytes, input_bytes = parse_blob(input_bytes)
    message = type(new_proof_of_space).from_bytes(message_bytes)
    assert message == new_proof_of_space
    assert bytes(message) == bytes(new_proof_of_space)

    message_bytes, input_bytes = parse_blob(input_bytes)
    message = type(request_signatures).from_bytes(message_bytes)
    assert message == request_signatures
    assert bytes(message) == bytes(request_signatures)

    message_bytes, input_bytes = parse_blob(input_bytes)
    message = type(respond_signatures).from_bytes(message_bytes)
    assert message == respond_signatures
    assert bytes(message) == bytes(respond_signatures)

    message_bytes, input_bytes = parse_blob(input_bytes)
    message = type(plot).from_bytes(message_bytes)
    assert message == plot
    assert bytes(message) == bytes(plot)

    message_bytes, input_bytes = parse_blob(input_bytes)
    message = type(request_plots).from_bytes(message_bytes)
    assert message == request_plots
    assert bytes(message) == bytes(request_plots)

    message_bytes, input_bytes = parse_blob(input_bytes)
    message = type(respond_plots).from_bytes(message_bytes)
    assert message == respond_plots
    assert bytes(message) == bytes(respond_plots)

    message_bytes, input_bytes = parse_blob(input_bytes)
    message = type(request_peers_introducer).from_bytes(message_bytes)
    assert message == request_peers_introducer
    assert bytes(message) == bytes(request_peers_introducer)

    message_bytes, input_bytes = parse_blob(input_bytes)
    message = type(respond_peers_introducer).from_bytes(message_bytes)
    assert message == respond_peers_introducer
    assert bytes(message) == bytes(respond_peers_introducer)

    message_bytes, input_bytes = parse_blob(input_bytes)
    message = type(authentication_payload).from_bytes(message_bytes)
    assert message == authentication_payload
    assert bytes(message) == bytes(authentication_payload)

    message_bytes, input_bytes = parse_blob(input_bytes)
    message = type(get_pool_info_response).from_bytes(message_bytes)
    assert message == get_pool_info_response
    assert bytes(message) == bytes(get_pool_info_response)

    message_bytes, input_bytes = parse_blob(input_bytes)
    message = type(post_partial_payload).from_bytes(message_bytes)
    assert message == post_partial_payload
    assert bytes(message) == bytes(post_partial_payload)

    message_bytes, input_bytes = parse_blob(input_bytes)
    message = type(post_partial_request).from_bytes(message_bytes)
    assert message == post_partial_request
    assert bytes(message) == bytes(post_partial_request)

    message_bytes, input_bytes = parse_blob(input_bytes)
    message = type(post_partial_response).from_bytes(message_bytes)
    assert message == post_partial_response
    assert bytes(message) == bytes(post_partial_response)

    message_bytes, input_bytes = parse_blob(input_bytes)
    message = type(get_farmer_response).from_bytes(message_bytes)
    assert message == get_farmer_response
    assert bytes(message) == bytes(get_farmer_response)

    message_bytes, input_bytes = parse_blob(input_bytes)
    message = type(post_farmer_payload).from_bytes(message_bytes)
    assert message == post_farmer_payload
    assert bytes(message) == bytes(post_farmer_payload)

    message_bytes, input_bytes = parse_blob(input_bytes)
    message = type(post_farmer_request).from_bytes(message_bytes)
    assert message == post_farmer_request
    assert bytes(message) == bytes(post_farmer_request)

    message_bytes, input_bytes = parse_blob(input_bytes)
    message = type(post_farmer_response).from_bytes(message_bytes)
    assert message == post_farmer_response
    assert bytes(message) == bytes(post_farmer_response)

    message_bytes, input_bytes = parse_blob(input_bytes)
    message = type(put_farmer_payload).from_bytes(message_bytes)
    assert message == put_farmer_payload
    assert bytes(message) == bytes(put_farmer_payload)

    message_bytes, input_bytes = parse_blob(input_bytes)
    message = type(put_farmer_request).from_bytes(message_bytes)
    assert message == put_farmer_request
    assert bytes(message) == bytes(put_farmer_request)

    message_bytes, input_bytes = parse_blob(input_bytes)
    message = type(put_farmer_response).from_bytes(message_bytes)
    assert message == put_farmer_response
    assert bytes(message) == bytes(put_farmer_response)

    message_bytes, input_bytes = parse_blob(input_bytes)
    message = type(error_response).from_bytes(message_bytes)
    assert message == error_response
    assert bytes(message) == bytes(error_response)

    message_bytes, input_bytes = parse_blob(input_bytes)
    message = type(new_peak_timelord).from_bytes(message_bytes)
    assert message == new_peak_timelord
    assert bytes(message) == bytes(new_peak_timelord)

    message_bytes, input_bytes = parse_blob(input_bytes)
    message = type(new_unfinished_block_timelord).from_bytes(message_bytes)
    assert message == new_unfinished_block_timelord
    assert bytes(message) == bytes(new_unfinished_block_timelord)

    message_bytes, input_bytes = parse_blob(input_bytes)
    message = type(new_infusion_point_vdf).from_bytes(message_bytes)
    assert message == new_infusion_point_vdf
    assert bytes(message) == bytes(new_infusion_point_vdf)

    message_bytes, input_bytes = parse_blob(input_bytes)
    message = type(new_signage_point_vdf).from_bytes(message_bytes)
    assert message == new_signage_point_vdf
    assert bytes(message) == bytes(new_signage_point_vdf)

    message_bytes, input_bytes = parse_blob(input_bytes)
    message = type(new_end_of_sub_slot_bundle).from_bytes(message_bytes)
    assert message == new_end_of_sub_slot_bundle
    assert bytes(message) == bytes(new_end_of_sub_slot_bundle)

    message_bytes, input_bytes = parse_blob(input_bytes)
    message = type(request_compact_proof_of_time).from_bytes(message_bytes)
    assert message == request_compact_proof_of_time
    assert bytes(message) == bytes(request_compact_proof_of_time)

    message_bytes, input_bytes = parse_blob(input_bytes)
    message = type(respond_compact_proof_of_time).from_bytes(message_bytes)
    assert message == respond_compact_proof_of_time
    assert bytes(message) == bytes(respond_compact_proof_of_time)

    assert input_bytes == b""