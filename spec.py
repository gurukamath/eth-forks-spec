"""
Ethereum Specification
^^^^^^^^^^^^^^^^^^^^^^

.. contents:: Table of Contents
    :backlinks: none
    :local:

Introduction
------------

Entry point for the Ethereum specification.
"""

from dataclasses import dataclass
from typing import List, Optional, Set, Tuple

from ethereum.crypto import SECP256K1N
from ethereum.ethash import dataset_size, generate_cache, hashimoto_light
from ethereum.utils.ensure import ensure

from .. import crypto, rlp
from ..base_types import U256, U256_CEIL_VALUE, Bytes, Uint
from . import vm
from .bloom import logs_bloom
from .eth_types import (
    TX_BASE_COST,
    TX_DATA_COST_PER_NON_ZERO,
    TX_DATA_COST_PER_ZERO,
    Address,
    Block,
    Bloom,
    Hash32,
    Header,
    Log,
    Receipt,
    Root,
    Transaction,
)
from .genesis import genesis_configuration
from .state import (
    State,
    create_ether,
    destroy_account,
    get_account,
    increment_nonce,
    set_account_balance,
    state_root,
)
from .trie import Trie, root, trie_set
from .utils.message import prepare_message
from .vm.interpreter import process_message_call

BLOCK_REWARD = U256(5 * 10 ** 18)
GAS_LIMIT_ADJUSTMENT_FACTOR = 1024
GAS_LIMIT_MINIMUM = 5000
GENESIS_DIFFICULTY = Uint(131072)
MAX_OMMER_DEPTH = 6


@dataclass
class BlockChain:
    """
    History and current state of the block chain.
    """

    blocks: List[Block]
    state: State


def apply_fork(old: BlockChain) -> BlockChain:
    """
    Transforms the state from the previous hard fork (`old`) into the block
    chain object for this hard fork and returns it.

    Parameters
    ----------
    old :
        Previous block chain object.

    Returns
    -------
    new : `BlockChain`
        Upgraded block chain object for this hard fork.
    """
    genesis = genesis_configuration("mainnet.json")

    for account, balance in genesis.initial_balances.items():
        create_ether(old.state, account, balance)

    genesis_header = Header(
        parent_hash=Hash32(b"\0" * 32),
        ommers_hash=rlp.rlp_hash(()),
        coinbase=Address(b"\0" * 20),
        state_root=state_root(old.state),
        transactions_root=root(Trie(False, None)),
        receipt_root=root(Trie(False, None)),
        bloom=Bloom(b"\0" * 256),
        difficulty=genesis.difficulty,
        number=Uint(0),
        gas_limit=genesis.gas_limit,
        gas_used=Uint(0),
        timestamp=genesis.timestamp,
        extra_data=genesis.extra_data,
        mix_digest=Hash32(b"\0" * 32),
        nonce=genesis.nonce,
    )

    genesis_block = Block(
        header=genesis_header,
        transactions=(),
        ommers=(),
    )

    old.blocks.append(genesis_block)

    return old


def get_last_256_block_hashes(chain: BlockChain) -> List[Hash32]:
    """
    Obtain the list of hashes of the previous 256 blocks in order of increasing
    block number.

    This function will return less hashes for the first 256 blocks.

    Parameters
    ----------
    chain :
        History and current state.

    Returns
    -------
    recent_block_hashes : `List[Hash32]`
        Hashes of the recent 256 blocks in order of increasing block number.
    """
    recent_blocks = chain.blocks[-255:]
    # TODO: This function has not been tested rigorously
    if len(recent_blocks) == 0:
        return []

    recent_block_hashes = []

    for block in recent_blocks:
        prev_block_hash = block.header.parent_hash
        recent_block_hashes.append(prev_block_hash)

    # We are computing the hash only for the most recent block and not for
    # the rest of the blocks as they have successors which have the hash of
    # the current block as parent hash.
    most_recent_block_hash = crypto.keccak256(
        rlp.encode(recent_blocks[-1].header)
    )
    recent_block_hashes.append(most_recent_block_hash)

    return recent_block_hashes


def state_transition(chain: BlockChain, block: Block) -> None:
    """
    Attempts to apply a block to an existing block chain.

    Parameters
    ----------
    chain :
        History and current state.
    block :
        Block to apply to `chain`.
    """
    parent_header = chain.blocks[-1].header
    validate_header(block.header, parent_header)
    validate_ommers(block.ommers, block.header, chain)
    (
        gas_used,
        transactions_root,
        receipt_root,
        block_logs_bloom,
        state,
    ) = apply_body(
        chain.state,
        get_last_256_block_hashes(chain),
        block.header.coinbase,
        block.header.number,
        block.header.gas_limit,
        block.header.timestamp,
        block.header.difficulty,
        block.transactions,
        block.ommers,
    )
    ensure(gas_used == block.header.gas_used)
    ensure(transactions_root == block.header.transactions_root)
    ensure(state_root(state) == block.header.state_root)
    ensure(receipt_root == block.header.receipt_root)
    ensure(block_logs_bloom == block.header.bloom)

    chain.blocks.append(block)
    if len(chain.blocks) > 255:
        # Real clients have to store more blocks to deal with reorgs, but the
        # protocol only requires the last 255
        chain.blocks = chain.blocks[-255:]


def validate_header(header: Header, parent_header: Header) -> None:
    """
    Verifies a block header.

    Parameters
    ----------
    header :
        Header to check for correctness.
    parent_header :
        Parent Header of the header to check for correctness
    """
    block_difficulty = calculate_block_difficulty(
        header.number,
        header.timestamp,
        parent_header.timestamp,
        parent_header.difficulty,
    )

    block_parent_hash = crypto.keccak256(rlp.encode(parent_header))

    ensure(header.parent_hash == block_parent_hash)
    ensure(header.difficulty == block_difficulty)
    ensure(header.number == parent_header.number + 1)
    ensure(check_gas_limit(header.gas_limit, parent_header.gas_limit))
    ensure(header.timestamp > parent_header.timestamp)
    ensure(len(header.extra_data) <= 32)

    validate_proof_of_work(header)


def generate_header_hash_for_pow(header: Header) -> Hash32:
    """
    Generate rlp hash of the header which is to be used for Proof-of-Work
    verification. This hash is generated with the following header fields:

      * `parent_hash`
      * `ommers_hash`
      * `coinbase`
      * `state_root`
      * `transactions_root`
      * `receipt_root`
      * `bloom`
      * `difficulty`
      * `number`
      * `gas_limit`
      * `gas_used`
      * `timestamp`
      * `extra_data`

    In other words, the PoW artefacts `mix_digest` and `nonce` are ignored
    while calculating this hash.

    Parameters
    ----------
    header :
        The header object for which the hash is to be generated.

    Returns
    -------
    hash : `Hash32`
        The PoW valid rlp hash of the passed in header.
    """
    header_data_without_pow_artefacts = [
        header.parent_hash,
        header.ommers_hash,
        header.coinbase,
        header.state_root,
        header.transactions_root,
        header.receipt_root,
        header.bloom,
        header.difficulty,
        header.number,
        header.gas_limit,
        header.gas_used,
        header.timestamp,
        header.extra_data,
    ]

    return rlp.rlp_hash(header_data_without_pow_artefacts)


def validate_proof_of_work(header: Header) -> None:
    """
    Validates the Proof of Work constraints.

    Parameters
    ----------
    header :
        Header of interest.
    """
    header_hash = generate_header_hash_for_pow(header)
    # TODO: Memoize this somewhere and read from that data instead of
    # calculating cache for every block validation.
    cache = generate_cache(header.number)
    mix_digest, result = hashimoto_light(
        header_hash, header.nonce, cache, dataset_size(header.number)
    )

    ensure(mix_digest == header.mix_digest)
    ensure(
        Uint.from_be_bytes(result) <= (U256_CEIL_VALUE // header.difficulty)
    )


def apply_body(
    state: State,
    block_hashes: List[Hash32],
    coinbase: Address,
    block_number: Uint,
    block_gas_limit: Uint,
    block_time: U256,
    block_difficulty: Uint,
    transactions: Tuple[Transaction, ...],
    ommers: Tuple[Header, ...],
) -> Tuple[Uint, Root, Root, Bloom, State]:
    """
    Executes a block.

    Parameters
    ----------
    state :
        Current account state.
    block_hashes :
        List of hashes of the previous 256 blocks in the order of
        increasing block number.
    coinbase :
        Address of account which receives block reward and transaction fees.
    block_number :
        Position of the block within the chain.
    block_gas_limit :
        Initial amount of gas available for execution in this block.
    block_time :
        Time the block was produced, measured in seconds since the epoch.
    block_difficulty :
        Difficulty of the block.
    transactions :
        Transactions included in the block.
    ommers :
        Headers of ancestor blocks which are not direct parents (formerly
        uncles.)

    Returns
    -------
    gas_available : `eth1spec.base_types.Uint`
        Remaining gas after all transactions have been executed.
    transactions_root : `eth1spec.eth_types.Root`
        Trie root of all the transactions in the block.
    receipt_root : `eth1spec.eth_types.Root`
        Trie root of all the receipts in the block.
    block_logs_bloom : `Bloom`
        Logs bloom of all the logs included in all the transactions of the
        block.
    state : `eth1spec.eth_types.State`
        State after all transactions have been executed.
    """
    gas_available = block_gas_limit
    transactions_trie: Trie[Bytes, Optional[Transaction]] = Trie(
        secured=False, default=None
    )
    receipts_trie: Trie[Bytes, Optional[Receipt]] = Trie(
        secured=False, default=None
    )
    block_logs: Tuple[Log, ...] = ()

    for i, tx in enumerate(transactions):
        trie_set(transactions_trie, rlp.encode(Uint(i)), tx)

        ensure(tx.gas <= gas_available)
        sender_address = recover_sender(tx)

        env = vm.Environment(
            caller=sender_address,
            origin=sender_address,
            block_hashes=block_hashes,
            coinbase=coinbase,
            number=block_number,
            gas_limit=block_gas_limit,
            gas_price=tx.gas_price,
            time=block_time,
            difficulty=block_difficulty,
            state=state,
        )

        gas_used, logs = process_transaction(env, tx)
        gas_available -= gas_used

        trie_set(
            receipts_trie,
            rlp.encode(Uint(i)),
            Receipt(
                post_state=state_root(state),
                cumulative_gas_used=(block_gas_limit - gas_available),
                bloom=logs_bloom(logs),
                logs=logs,
            ),
        )
        block_logs += logs

    pay_rewards(state, block_number, coinbase, ommers)

    gas_remaining = block_gas_limit - gas_available

    block_logs_bloom = logs_bloom(block_logs)

    return (
        gas_remaining,
        root(transactions_trie),
        root(receipts_trie),
        block_logs_bloom,
        state,
    )


def validate_ommers(
    ommers: Tuple[Header, ...], block_header: Header, chain: BlockChain
) -> None:
    """
    Validates the ommers mentioned in the block.

    Parameters
    ----------
    ommers :
        List of ommers mentioned in the current block.
    block_header:
        The header of current block.
    chain :
        History and current state.
    """
    block_hash = rlp.rlp_hash(block_header)

    ensure(rlp.rlp_hash(ommers) == block_header.ommers_hash)

    if len(ommers) == 0:
        # Nothing to validate
        return

    # Check that each ommer satisfies the constraints of a header
    for ommer in ommers:
        ensure(1 <= ommer.number < block_header.number)
        ommer_parent_header = chain.blocks[
            -(block_header.number - ommer.number) - 1
        ].header
        validate_header(ommer, ommer_parent_header)

    # Check that there can be only at most 2 ommers for a block.
    ensure(len(ommers) <= 2)

    ommers_hashes = [rlp.rlp_hash(ommer) for ommer in ommers]
    # Check that there are no duplicates in the ommers of current block
    ensure(len(ommers_hashes) == len(set(ommers_hashes)))

    recent_canonical_blocks = chain.blocks[-(MAX_OMMER_DEPTH + 1) :]
    recent_canonical_block_hashes = {
        rlp.rlp_hash(block.header) for block in recent_canonical_blocks
    }
    recent_ommers_hashes: Set[Hash32] = set()
    for block in recent_canonical_blocks:
        recent_ommers_hashes = recent_ommers_hashes.union(
            {rlp.rlp_hash(ommer) for ommer in block.ommers}
        )

    for ommer_index, ommer in enumerate(ommers):
        ommer_hash = ommers_hashes[ommer_index]
        # The current block shouldn't be the ommer
        ensure(ommer_hash != block_hash)

        # Ommer shouldn't be one of the recent canonical blocks
        ensure(ommer_hash not in recent_canonical_block_hashes)

        # Ommer shouldn't be one of the uncles mentioned in the recent
        # canonical blocks
        ensure(ommer_hash not in recent_ommers_hashes)

        # Ommer age with respect to the current block. For example, an age of
        # 1 indicates that the ommer is a sibling of previous block.
        ommer_age = block_header.number - ommer.number
        ensure(1 <= ommer_age <= MAX_OMMER_DEPTH)

        ensure(ommer.parent_hash in recent_canonical_block_hashes)
        ensure(ommer.parent_hash != block_header.parent_hash)


def pay_rewards(
    state: State,
    block_number: Uint,
    coinbase: Address,
    ommers: Tuple[Header, ...],
) -> None:
    """
    Pay rewards to the block miner as well as the ommers miners.

    Parameters
    ----------
    state :
        Current account state.
    block_number :
        Position of the block within the chain.
    coinbase :
        Address of account which receives block reward and transaction fees.
    ommers :
        List of ommers mentioned in the current block.
    """
    miner_reward = BLOCK_REWARD + (len(ommers) * (BLOCK_REWARD // 32))
    create_ether(state, coinbase, miner_reward)

    for ommer in ommers:
        # Ommer age with respect to the current block.
        ommer_age = U256(block_number - ommer.number)
        ommer_miner_reward = ((8 - ommer_age) * BLOCK_REWARD) // 8
        create_ether(state, ommer.coinbase, ommer_miner_reward)


def process_transaction(
    env: vm.Environment, tx: Transaction
) -> Tuple[U256, Tuple[Log, ...]]:
    """
    Execute a transaction against the provided environment.

    Parameters
    ----------
    env :
        Environment for the Ethereum Virtual Machine.
    tx :
        Transaction to execute.

    Returns
    -------
    gas_left : `eth1spec.base_types.U256`
        Remaining gas after execution.
    logs : `Tuple[eth1spec.eth_types.Log, ...]`
        Logs generated during execution.
    """
    ensure(validate_transaction(tx))

    sender = env.origin
    sender_account = get_account(env.state, sender)
    gas_fee = tx.gas * tx.gas_price
    ensure(sender_account.nonce == tx.nonce)
    ensure(sender_account.balance >= gas_fee)

    gas = tx.gas - calculate_intrinsic_cost(tx)
    increment_nonce(env.state, sender)
    sender_balance_after_gas_fee = sender_account.balance - gas_fee
    set_account_balance(env.state, sender, sender_balance_after_gas_fee)

    message = prepare_message(
        sender,
        tx.to,
        tx.value,
        tx.data,
        gas,
        env,
    )

    (
        gas_left,
        refund_counter,
        logs,
        accounts_to_delete,
        has_erred,
    ) = process_message_call(message, env)

    gas_used = tx.gas - gas_left
    gas_refund = min(gas_used // 2, refund_counter)
    gas_refund_amount = (gas_left + gas_refund) * tx.gas_price
    transaction_fee = (tx.gas - gas_left - gas_refund) * tx.gas_price
    total_gas_used = gas_used - gas_refund

    # refund gas
    sender_balance_after_refund = (
        get_account(env.state, sender).balance + gas_refund_amount
    )
    set_account_balance(env.state, sender, sender_balance_after_refund)

    # transfer miner fees
    coinbase_balance_after_mining_fee = (
        get_account(env.state, env.coinbase).balance + transaction_fee
    )
    set_account_balance(
        env.state, env.coinbase, coinbase_balance_after_mining_fee
    )

    for address in accounts_to_delete:
        destroy_account(env.state, address)

    return total_gas_used, logs


def validate_transaction(tx: Transaction) -> bool:
    """
    Verifies a transaction.

    Parameters
    ----------
    tx :
        Transaction to validate.

    Returns
    -------
    verified : `bool`
        True if the transaction can be executed, or False otherwise.
    """
    return calculate_intrinsic_cost(tx) <= tx.gas


def calculate_intrinsic_cost(tx: Transaction) -> Uint:
    """
    Calculates the intrinsic cost of the transaction that is charged before
    execution is instantiated.

    Parameters
    ----------
    tx :
        Transaction to compute the intrinsic cost of.

    Returns
    -------
    verified : `eth1spec.base_types.Uint`
        The intrinsic cost of the transaction.
    """
    data_cost = 0

    for byte in tx.data:
        if byte == 0:
            data_cost += TX_DATA_COST_PER_ZERO
        else:
            data_cost += TX_DATA_COST_PER_NON_ZERO

    return Uint(TX_BASE_COST + data_cost)


def recover_sender(tx: Transaction) -> Address:
    """
    Extracts the sender address from a transaction.

    Parameters
    ----------
    tx :
        Transaction of interest.

    Returns
    -------
    sender : `eth1spec.eth_types.Address`
        The address of the account that signed the transaction.
    """
    v, r, s = tx.v, tx.r, tx.s

    #  if v > 28:
    #      v = v - (chain_id*2+8)

    ensure(v == 27 or v == 28)
    ensure(0 < r and r < SECP256K1N)
    ensure(0 < s and s < SECP256K1N)

    public_key = crypto.secp256k1_recover(r, s, v - 27, signing_hash(tx))
    return Address(crypto.keccak256(public_key)[12:32])


def signing_hash(tx: Transaction) -> Hash32:
    """
    Compute the hash of a transaction used in the signature.

    Parameters
    ----------
    tx :
        Transaction of interest.

    Returns
    -------
    hash : `eth1spec.eth_types.Hash32`
        Hash of the transaction.
    """
    return crypto.keccak256(
        rlp.encode(
            (
                tx.nonce,
                tx.gas_price,
                tx.gas,
                tx.to,
                tx.value,
                tx.data,
            )
        )
    )


def compute_header_hash(header: Header) -> Hash32:
    """
    Computes the hash of a block header.

    Parameters
    ----------
    header :
        Header of interest.

    Returns
    -------
    hash : `ethereum.eth_types.Hash32`
        Hash of the header.
    """
    return crypto.keccak256(rlp.encode(header))


def get_block_header_by_hash(hash: Hash32, chain: BlockChain) -> Header:
    """
    Fetches the block header with the corresponding hash.

    Parameters
    ----------
    hash :
        Hash of the header of interest.

    chain :
        History and current state.

    Returns
    -------
    Header : `ethereum.eth_types.Header`
        Block header found by its hash.
    """
    for block in chain.blocks:
        if compute_header_hash(block.header) == hash:
            return block.header
    else:
        raise ValueError(f"Could not find header with hash={hash.hex()}")


def check_gas_limit(gas_limit: Uint, parent_gas_limit: Uint) -> bool:
    """
    Validates the gas limit for a block.

    Parameters
    ----------
    gas_limit :
        Gas limit to validate.

    parent_gas_limit :
        Gas limit of the parent block.

    Returns
    -------
    check : `bool`
        True if gas limit constraints are satisfied, False otherwise.
    """
    max_adjustment_delta = parent_gas_limit // GAS_LIMIT_ADJUSTMENT_FACTOR
    if gas_limit >= parent_gas_limit + max_adjustment_delta:
        return False
    if gas_limit <= parent_gas_limit - max_adjustment_delta:
        return False
    if gas_limit < GAS_LIMIT_MINIMUM:
        return False

    return True


def calculate_block_difficulty(
    number: Uint,
    timestamp: U256,
    parent_timestamp: U256,
    parent_difficulty: Uint,
) -> Uint:
    """
    Computes difficulty of a block using its header and parent header.

    Parameters
    ----------
    number :
        Block number of the block.
    timestamp :
        Timestamp of the block.
    parent_timestamp :
        Timestamp of the parent block.
    parent_difficulty :
        difficulty of the parent block.

    Returns
    -------
    difficulty : `ethereum.base_types.Uint`
        Computed difficulty for a block.
    """
    max_adjustment_delta = parent_difficulty // Uint(2048)
    if number == 0:
        return GENESIS_DIFFICULTY
    elif timestamp < parent_timestamp + 13:
        difficulty = parent_difficulty + max_adjustment_delta
    else:  # timestamp >= parent_timestamp + 13
        difficulty = parent_difficulty - max_adjustment_delta

    # Historical Note: The difficulty bomb was not present in Ethereum at the
    # start of Frontier, but was added shortly after launch. However since the
    # bomb has no effect prior to block 200000 we pretend it existed from
    # genesis.
    # See https://github.com/ethereum/go-ethereum/pull/1588
    num_bomb_periods = int(number) // 100000 - 2
    if num_bomb_periods >= 0:
        difficulty += 2 ** num_bomb_periods
    return Uint(max(difficulty, GENESIS_DIFFICULTY))
