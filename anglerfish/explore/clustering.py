import pandas as pd
import Levenshtein as lev
import numpy as np
import argparse
from sklearn.cluster import AgglomerativeClustering


def cluster_indexes(hits_df, distance_threshold=2, min_cluster_pct=0.01):
    # Simplistic clustering using levenshtein distances

    # get inserts from df_good_hits cs strings
    inserts = hits_df.cs.str.extract(r"^cs:Z::[1-9][0-9]*\+([a,c,t,g]*):[1-9][0-9]*$")[
        0
    ]
    # calculate distances as a matrix
    # TODO try sequence-levenshtein, also this is not very efficient
    distances = pd.DataFrame(
        index=inserts.index, columns=inserts.index, dtype=float
    ).fillna(0)
    for i, insert1 in inserts.items():
        for j, insert2 in inserts.items():
            distances.loc[i, j] = lev.distance(insert1, insert2)

    # cluster scikit-learn agglomerative clustering
    # https://scikit-learn.org/stable/modules/generated/sklearn.cluster.AgglomerativeClustering.html

    clustering = AgglomerativeClustering(
        metric="precomputed",
        n_clusters=None,
        distance_threshold=distance_threshold,
        linkage="average",
    ).fit(distances)

    hits_df["cluster"] = clustering.labels_

    # Get the most frequent insert from inserts df for each cluster
    cluster_sizes = hits_df.cluster.value_counts()
    hits_df["cluster"] = hits_df["cluster"].apply(
        lambda x: x if cluster_sizes[x] > len(hits_df) * min_cluster_pct else -1
    )

    # Get the most frequent insert from inserts df for each cluster
    hits_df["insert"] = hits_df["cluster"].apply(
        lambda x: inserts[hits_df.cluster == x].mode().values[0] if x != -1 else "NA"
    )
    return hits_df


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Cluster inserts")
    parser.add_argument(
        "-i",
        "--input",
        help="Input file",
        required=True,
        type=str,
    )
    parser.add_argument(
        "-t",
        "--threshold",
        help="Distance threshold",
        required=False,
        type=int,
        default=2,
    )
    parser.add_argument(
        "-p",
        "--min_cluster_pct",
        help="Minimum cluster percentage",
        required=False,
        type=float,
        default=0.01,
    )

    args = parser.parse_args()

    hits_df = pd.read_csv(args.input)
    hits_df = cluster_indexes(hits_df, args.threshold, args.min_cluster_pct)

    print(hits_df.to_csv(index=False))
