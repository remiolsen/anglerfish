import glob
import io
import logging
import os
import re
import subprocess
from typing import cast

import Levenshtein as lev
from Bio.Seq import Seq
from Bio.SeqIO.QualityIO import FastqGeneralIterator

from anglerfish.demux.adaptor import Adaptor

log = logging.getLogger("anglerfish")


def parse_cs(
    cs_string: str,
    index_seq: str,
    umi_before: int | None = 0,
    umi_after: int | None = 0,
) -> tuple[str, int]:
    """
    Given a cs string, an index sequence, and optional UMI lengths:

    - Parse the cs string to find the suspected index region of the read.
    - Return a tuple of the given index sequence and it's Levenshtein distance
        to the parsed index region of the read.
    """
    # Create pattern for a substitution from "n" in the adaptor to a base in the read
    n_subbed_pattern = re.compile(r"\*n([atcg])")
    # Concatenate all n-to-base substitutions to yield the sequence of the read spanning the mask
    bases_spanning_mask = "".join(re.findall(n_subbed_pattern, cs_string))
    # Trim away any UMIs
    if umi_before is not None and umi_before > 0:
        bases_spanning_index_mask = bases_spanning_mask[umi_before:]
    elif umi_after is not None and umi_after > 0:
        bases_spanning_index_mask = bases_spanning_mask[:-umi_after]
    else:
        bases_spanning_index_mask = bases_spanning_mask
    # Return the index and the Levenshtein distance between it and the presumed index region of the read
    return bases_spanning_index_mask, lev.distance(
        index_seq.lower(), bases_spanning_index_mask
    )


def run_minimap2(
    fastq_in: str,
    index_file: str,
    output_paf: str,
    threads: int,
    minimap_b: int = 1,
):
    """
    Runs Minimap2
    """
    cmd: list[str] = [
        "minimap2",
        "--cs",  # Output the cs tag (short)
        "-c",  # Output cigar string in .paf
        *["-A", "6"],  # Matching score
        *["-B", str(minimap_b)],  # Mismatch penalty
        *["-k", "10"],  # k-mer size
        *["-m", "8"],  # Minimal chaining score
        *["-t", str(threads)],  # Number of threads
        *["-w", "5"],  # Minimizer window size
        index_file,  # Target
        fastq_in,  # Query
    ]

    run_log = f"{output_paf}.log"
    with open(output_paf, "ab") as ofile, open(run_log, "ab") as log_file:
        p1 = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=log_file)
        subprocess.run("sort", stdin=p1.stdout, stdout=ofile, check=True)


def parse_paf_lines(
    paf_path: str, min_qual: int = 1, complex_identifier: bool = False
) -> dict[str, list[dict]]:
    """
    Read and parse one paf alignment lines.
    Returns a dict with the import values for later use.

    If complex_identifier is True (default False), the keys will be on the form
    "{read}_{i5_or_i7}_{strand_str}".
    """
    entries: dict = {}
    with open(paf_path) as paf:
        for paf_line in paf:
            paf_cols = paf_line.split()
            try:
                # TODO: objectify this

                # Unpack cols to vars for type annotation
                read: str = paf_cols[0]
                adapter: str = paf_cols[5]
                rlen: int = int(paf_cols[1])  # read length
                rstart: int = int(paf_cols[2])  # start alignment on read
                rend: int = int(paf_cols[3])  # end alignment on read
                strand: str = paf_cols[4]
                cg: str = paf_cols[-2]  # cigar string
                cs: str = paf_cols[-1]  # cigar diff string
                q: int = int(paf_cols[11])  # Q score
                iseq: str | None = None
                sample: str | None = None

                # Determine identifier
                if complex_identifier:
                    i5_or_i7 = adapter.split("_")[-1]
                    if strand == "+":
                        strand_str = "positive"
                    else:
                        strand_str = "negative"
                    key = f"{read}_{i5_or_i7}_{strand_str}"
                else:
                    key = read

            except IndexError:
                log.debug(f"Could not find all paf columns: {read}")
                continue

            if q < min_qual:
                log.debug(f"Low quality alignment: {read}")
                continue

            # Compile entry
            entry = {
                "read": read,
                "adapter": adapter,
                "rlen": rlen,
                "rstart": rstart,
                "rend": rend,
                "strand": strand,
                "cg": cg,
                "cs": cs,
                "q": q,
                "iseq": iseq,
                "sample": sample,
            }

            if key in entries.keys():
                entries[key].append(entry)
            else:
                entries[key] = [entry]

    return entries


def layout_matches(
    i5_name: str, i7_name: str, paf_entries: dict[str, list[dict]]
) -> tuple[dict, dict, dict, dict]:
    """
    Search the parsed paf alignments and layout possible Illumina library fragments
    Returns dicts:
        - fragments. Reads with one I7 and one I5
        - singletons. Reads with that only match either I5 or I7 adaptors
        - concats. Concatenated fragments. Fragments with several alternating I7, I5 matches
        - unknowns. Any other reads, but usually i5-i5 or i7-i7 matches
    """

    fragments = {}
    singletons = {}
    concats = {}
    unknowns = {}
    for read, entry_list in paf_entries.items():
        sorted_entries = []
        for k in range(len(entry_list) - 1):
            entry_i = entry_list[k]
            entry_j = entry_list[k + 1]
            if (
                entry_i["adapter"] != entry_j["adapter"]
                and (entry_i["adapter"] == i5_name and entry_j["adapter"] == i7_name)
                or (entry_j["adapter"] == i5_name and entry_i["adapter"] == i7_name)
            ):
                if entry_i in sorted_entries:
                    sorted_entries.append(entry_j)
                else:
                    sorted_entries.extend([entry_i, entry_j])
        if len(entry_list) == 1:
            singletons[read] = entry_list
        elif len(sorted_entries) == 2:
            fragments[read] = sorted(sorted_entries, key=lambda l: l["rstart"])
        elif len(sorted_entries) > 2:
            concats[read] = sorted(sorted_entries, key=lambda l: l["rstart"])
            log.debug(
                f"Concatenated fragment: {read}, found: {[(i['adapter'],i['rstart']) for i in sorted_entries]}"
            )
        else:
            unknowns[read] = entry_list
            log.debug(
                f"Unknown fragment: {read}, found: {[(i['adapter'],i['rstart']) for i in entry_list]}"
            )
        # TODO: add minimum insert size
    return (fragments, singletons, concats, unknowns)


def cluster_matches(
    sample_adaptor: list[tuple[str, Adaptor, str]],
    matches: dict,
    max_distance: int,
    i7_reversed: bool = False,
    i5_reversed: bool = False,
) -> tuple[list, list]:
    # Only illumina fragments
    matched = {}
    matched_bed = []
    unmatched_bed = []
    for read, alignments in matches.items():
        if (
            alignments[0]["adapter"][-2:] == "i5"
            and alignments[1]["adapter"][-2:] == "i7"
        ):
            i5 = alignments[0]
            i7 = alignments[1]
        elif (
            alignments[1]["adapter"][-2:] == "i5"
            and alignments[0]["adapter"][-2:] == "i7"
        ):
            i5 = alignments[1]
            i7 = alignments[0]
        else:
            log.debug(" {read} has no valid illumina fragment")
            continue

        dists = []
        fi5 = ""
        fi7 = ""
        for _, adaptor, _ in sample_adaptor:
            if adaptor.i5.index_seq is not None:
                i5_seq = adaptor.i5.index_seq
                if i5_reversed and i5_seq is not None:
                    i5_seq = str(Seq(i5_seq).reverse_complement())
                fi5, d1 = parse_cs(
                    i5["cs"],
                    i5_seq,
                    adaptor.i5.len_umi_before_index,
                    adaptor.i5.len_umi_after_index,
                )
            else:
                d1 = 0

            if adaptor.i7.index_seq is not None:
                i7_seq = adaptor.i7.index_seq
                if i7_reversed and i7_seq is not None:
                    i7_seq = str(Seq(i7_seq).reverse_complement())
                fi7, d2 = parse_cs(
                    i7["cs"],
                    i7_seq,
                    adaptor.i7.len_umi_before_index,
                    adaptor.i7.len_umi_after_index,
                )
            else:
                d2 = 0

            dists.append(d1 + d2)

        index_min = min(range(len(dists)), key=dists.__getitem__)
        # Test if two samples in the sheet is equidistant to the i5/i7
        if len([i for i, j in enumerate(dists) if j == dists[index_min]]) > 1:
            continue
        start_insert = min(i5["rend"], i7["rend"])
        end_insert = max(i7["rstart"], i5["rstart"])
        if end_insert - start_insert < 10:
            continue
        if dists[index_min] > max_distance:
            # Find only full length i7(+i5) adaptor combos. Basically a list of "known unknowns"
            if len(fi7) + len(fi5) == len(adaptor.i7.index_seq or "") + len(
                adaptor.i5.index_seq or ""
            ):
                fi75 = "+".join([i for i in [fi7, fi5] if not i == ""])
                unmatched_bed.append([read, start_insert, end_insert, fi75, "999", "."])
            continue
        matched[read] = alignments
        matched_bed.append(
            [read, start_insert, end_insert, sample_adaptor[index_min][0], "999", "."]
        )
    log.debug(f" Matched {len(matched)} reads, unmatched {len(unmatched_bed)} reads")
    return unmatched_bed, matched_bed


def write_demuxedfastq(
    beds: dict[str, list], fastq_in: os.PathLike, fastq_out: os.PathLike
) -> int:
    """
    Intended for multiprocessing
    Take a set of coordinates in bed format [[seq1, start, end, ..][seq2, ..]]
    from over a set of fastq entries in the input files and do extraction.

    Return: PID of the process
    """

    gz_buf = 131072
    fq_files = cast(list[str], glob.glob(fastq_in))
    assert len(fq_files) > 0, f"No fastq files found looking for {fastq_in}."
    for fq in fq_files:
        with subprocess.Popen(
            ["gzip", "-c", "-d", fq], stdout=subprocess.PIPE, bufsize=gz_buf
        ) as fzi:
            assert isinstance(fzi, subprocess.Popen)
            fi = io.TextIOWrapper(fzi.stdout, write_through=True)
            with open(fastq_out, "ab") as ofile:
                with subprocess.Popen(
                    ["gzip", "-c", "-f"],
                    stdin=subprocess.PIPE,
                    stdout=ofile,
                    bufsize=gz_buf,
                    close_fds=False,
                ) as oz:
                    assert isinstance(oz, subprocess.Popen)
                    for title, seq, qual in FastqGeneralIterator(fi):
                        new_title = title.split()
                        if new_title[0] not in beds.keys():
                            continue
                        outfqs = ""
                        for bed in beds[new_title[0]]:
                            new_title[0] += "_" + bed[3]
                            outfqs += "@{}\n".format(" ".join(new_title))
                            outfqs += f"{seq[bed[1] : bed[2]]}\n"
                            outfqs += "+\n"
                            outfqs += f"{qual[bed[1] : bed[2]]}\n"
                        oz.stdin.write(outfqs.encode("utf-8"))

    return os.getpid()
