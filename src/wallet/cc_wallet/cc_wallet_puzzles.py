from typing import Optional, Tuple

from clvm_tools import binutils
from clvm_tools.clvmc import compile_clvm
import re
import clvm
import string
from src.types.BLSSignature import BLSSignature
from src.types.program import Program
from src.types.coin import Coin
from src.types.coin_solution import CoinSolution


# This is for spending an existing coloured coin
from src.types.sized_bytes import bytes32
from src.types.spend_bundle import SpendBundle
from src.util.ints import uint64


def cc_make_puzzle(innerpuzhash, core):
    # Puzzle runs the core, but stores innerpuzhash commitment
    puzstring = f"(r (c (q 0x{innerpuzhash}) ((c (q {core}) (a)))))"
    result = Program(binutils.assemble(puzstring))
    return result


# Makes a core given a genesisID (aka the "colour")
def cc_make_core(originID):
    # solution is f"({core} {parent_str} {my_amount} {innerpuzreveal} {innersol} {auditor_info} {aggees})"
    # parent_str is either an atom or list depending on the type of spend
    # auditor is (primary_input, innerpuzzlehash, amount)
    # aggees is left blank if you aren't the auditor otherwise it is a list of
    # (primary_input, innerpuzhash, coin_amount, output_amount) for every coin in the spend
    # Compiled from coloured_coins.clvm
    compile_clvm('src/wallet/cc_wallet/coloured_coins.clvm', 'src/wallet/cc_wallet/coloured_coins.clvm.hex')
    clvm_hex = open('src/wallet/cc_wallet/coloured_coins.clvm.hex', "rt").read()
    clvm_blob = bytes.fromhex(clvm_hex)
    program = Program.from_bytes(clvm_blob)
    code = binutils.disassemble(program)
    core = re.sub('"REPLACE_ME_WITH_GENESIS_ID"', '0x' + str(originID), code)
    return core


def cc_make_eve_solution(parent_id: bytes32, full_puzzlehash: bytes32, amount: uint64):
    sol = f"(() 0x{parent_id} {amount} 0x{full_puzzlehash} () () ())"
    return Program(binutils.assemble(sol))


# This is for spending a recieved coloured coin
def cc_make_solution(
    core: str,
    parent_info: Tuple[bytes32, bytes32, uint64],
    amount: uint64,
    innerpuzreveal: str,
    innersol: str,
    auditor: Optional[Tuple[bytes32, bytes32, uint64]],
    auditees=None,
    genesis=False,
):
    parent_str = ""
    # parent_info is a triplet if parent was coloured or an atom if parent was genesis coin or we're a printed 0 val
    # genesis coin isn't coloured, child of genesis uses originID, all subsequent children use triplets
    # auditor is (primary_input, innerpuzzlehash, amount)
    # aggees should be (primary_input, innerpuzhash, coin_amount, output_amount)
    if not genesis:
        #  (parent primary input, parent inner puzzle hash, parent amount)
        if parent_info[1][0:2] == "0x":
            parent_str = f"(0x{parent_info[0]} {parent_info[1]} {parent_info[2]})"
        else:
            parent_str = f"(0x{parent_info[0]} 0x{parent_info[1]} {parent_info[2]})"
    else:
        parent_str = f"0x{parent_info[0].hex()}"

    auditor_formatted = "()"
    if auditor is not None:
        auditor_formatted = f"(0x{auditor[0]} 0x{auditor[1]} {auditor[2]})"

    aggees = "("
    if auditees is not None:
        for auditee in auditees:
            aggees = (
                aggees + f"(0x{auditee[0]} 0x{auditee[1]} {auditee[2]} {auditee[3]})"
            )

    aggees = aggees + ")"

    sol = f"(0x{Program(binutils.assemble(core)).get_tree_hash()} {parent_str} {amount} {innerpuzreveal} {innersol} {auditor_formatted} {aggees})"  # type: ignore # noqa
    return Program(binutils.assemble(sol))


def get_genesis_from_puzzle(puzzle: str):
    return puzzle[-2687:].split(")")[0]


def get_genesis_from_core(core: str):
    path = '(f (r (f (r (r (f (r (f (r (f (r (r (f (f (r (f (r (f (r (r (f (q {}))))))))))))))))))))))'
    cost, val = clvm.run_program(Program(binutils.assemble(path.format(core))), None)
    genesis = binutils.disassemble(val)[2:]
    return genesis


def get_innerpuzzle_from_puzzle(puzzle: str):
    return puzzle[9:75]


# Make sure that a generated E lock is spent in the spendbundle
def create_spend_for_ephemeral(parent_of_e, auditor_coin, spend_amount):
    puzstring = f"(r (r (c (q 0x{auditor_coin.name()}) (c (q {spend_amount}) (q ())))))"
    puzzle = Program(binutils.assemble(puzstring))
    coin = Coin(parent_of_e.name(), puzzle.get_tree_hash(), 0)
    solution = Program(binutils.assemble("()"))
    coinsol = CoinSolution(coin, clvm.to_sexp_f([puzzle, solution]))
    return coinsol


# Make sure that a generated A lock is spent in the spendbundle
def create_spend_for_auditor(parent_of_a, auditee):
    puzstring = f"(r (c (q 0x{auditee.name()}) (q ())))"
    puzzle = Program(binutils.assemble(puzstring))
    coin = Coin(parent_of_a.name(), puzzle.get_tree_hash(), 0)
    solution = Program(binutils.assemble("()"))
    coinsol = CoinSolution(coin, clvm.to_sexp_f([puzzle, solution]))
    return coinsol


def cc_generate_eve_spend(coin: Coin, full_puzzle: Program):
    solution = cc_make_eve_solution(
        coin.parent_coin_info, coin.puzzle_hash, coin.amount
    )
    list_of_solutions = [CoinSolution(coin, clvm.to_sexp_f([full_puzzle, solution,]),)]
    aggsig = BLSSignature.aggregate([])
    spend_bundle = SpendBundle(list_of_solutions, aggsig)
    return spend_bundle


# Returns the relative difference in value between the amount outputted by a puzzle and solution and a coin's amount
def get_output_discrepancy_for_puzzle_and_solution(coin, puzzle, solution):
    discrepancy = coin.amount - get_output_amount_for_puzzle_and_solution(
        puzzle, solution
    )
    return discrepancy

    # Returns the amount of value outputted by a puzzle and solution


def get_output_amount_for_puzzle_and_solution(puzzle, solution):
    conditions = clvm.run_program(puzzle, solution)[1]
    amount = 0
    while conditions != b"":
        opcode = conditions.first().first()
        if opcode == b"3":  # Check if CREATE_COIN
            amount_str = binutils.disassemble(conditions.first().rest().rest().first())
            if amount_str == "()":
                conditions = conditions.rest()
                continue
            elif amount_str[0:2] == "0x":  # Check for wonky decompilation
                amount += int(amount_str, 16)
            else:
                amount += int(amount_str, 10)
        conditions = conditions.rest()
    return amount


# inspect puzzle and check it is a CC puzzle
def check_is_cc_puzzle(puzzle: Program):
    puzzle_string = binutils.disassemble(puzzle)
    if len(puzzle_string) < 4000:
        return False
    inner_puzzle = puzzle_string[11:75]
    if all(c in string.hexdigits for c in inner_puzzle) is not True:
        return False
    genesisCoin = get_genesis_from_puzzle(puzzle_string)
    if all(c in string.hexdigits for c in genesisCoin) is not True:
        return False
    if cc_make_puzzle(inner_puzzle, cc_make_core(genesisCoin)) == puzzle:
        return True
    else:
        return False
