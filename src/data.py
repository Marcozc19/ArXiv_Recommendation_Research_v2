from collections import defaultdict
import json
import os
import sys
import time
import pandas as pd
from tqdm import tqdm
import requests

DATA_DIR = "/home/loaner/workspace/data"
DATASET_START_YEAR = 2011
DATASET_END_YEAR = 2021
CITATION_YEARS = 3
QUERY_BASE_DELAY = 0.1
QUERY_MULT_DELAY = 1.2
QUERY_DUMP_INTERVAL = 10

def kaggle_json_to_parquet():
    with open(os.path.join(DATA_DIR, "arxiv-metadata-oai-snapshot.json")) as f:
        kaggle_data = []
        for l in tqdm(f.readlines(), "Parsing json"):
            l = json.loads(l)
            if "cs." in l["categories"]:
                categories = l["categories"].split()
                for c in categories:
                    if c.startswith("cs"):
                        l["categories"] = categories
                        kaggle_data.append(l)
                        break
        kaggle_data = pd.DataFrame(kaggle_data)
        kaggle_data.to_parquet(os.path.join(DATA_DIR, "kaggle_data.parquet"))
        print(kaggle_data)


def get_citing_authors(l: list, paper_year: int, citation_years: int = CITATION_YEARS):
    authors = set()
    for citation in l:
        try:
            year = int(citation["year"])
            if year < paper_year + citation_years:
                for author in citation["authors"]:
                    authors.add(int(author["authorId"]))
        except TypeError:
            # catch year is null
            continue
    return list(authors)


def batch_query(
    json_save_path,
    query_ids,
    batch_size,
    query_fields,
    query_url,
    process_response_f
):
    if os.path.exists(json_save_path):
        with open(json_save_path) as f:
            data = json.load(f)
    else:
        data = {}

    print(f"Number of query ids: {len(query_ids)}")
    filtered_query_ids = [id for id in query_ids if id not in data.keys()]
    print(f"Ids without info: {len(filtered_query_ids)}")

    delay = QUERY_BASE_DELAY
    pbar = tqdm(total=len(query_ids))
    pbar.update(len(query_ids) - len(filtered_query_ids))
    idx = 0
    while idx < len(filtered_query_ids):
        batch_ids = filtered_query_ids[idx: idx + batch_size]
        response = requests.post(
            query_url,
            params={'fields': query_fields},
            json={"ids": batch_ids}
        )
        if response.status_code != 200:
            if "error" in response.text:
                print(response.text)
                sys.exit(1)
            delay *= QUERY_MULT_DELAY
            print(f" - Sleeping for {delay} seconds")
            if "Too Many Requests" not in response.text:
                print(json.loads(response.text))
            time.sleep(delay)
            continue

        for id, response in zip(batch_ids, response.json()):
            if response is None:
                print(f"{id} returned None")
                data[id] = None
                continue
            data[id] = process_response_f(response)
        
        idx += batch_size
        pbar.update(batch_size)
        delay = max(QUERY_BASE_DELAY, delay / QUERY_MULT_DELAY)

        if idx % (batch_size * QUERY_DUMP_INTERVAL) == 0:
            print(f"Dumping data to {json_save_path}")
            with open(json_save_path, 'w') as f:
                json.dump(data, f, indent=4)
        time.sleep(delay)
    pbar.close()

    with open(json_save_path, 'w') as f:
        json.dump(data, f, indent=4)


def prepare_papers_data():
    kaggle_data = pd.read_parquet(os.path.join(DATA_DIR, "kaggle_data.parquet"))
    print(f"Filtering relevant years (year < {DATASET_END_YEAR}) & (year >= {DATASET_START_YEAR})")
    kaggle_data['update_date'] = pd.to_datetime(kaggle_data['update_date'])
    kaggle_data['year_updated'] = kaggle_data['update_date'].dt.year
    kaggle_data = kaggle_data[(kaggle_data["year_updated"] < DATASET_END_YEAR) & (kaggle_data["year_updated"] >= DATASET_START_YEAR)]

    # Work on few papers
    kaggle_data = kaggle_data.sample(frac=1., random_state=0)
    kaggle_data = kaggle_data[:20]
    
    def process_paper_response(j: json):
        if j["year"] is None:
            return None
        j["s2FieldsOfStudy"] = list(set([tmp["category"] for tmp in j["s2FieldsOfStudy"] if tmp["category"] is not None]))
        j["authors"] = list(set([int(tmp["authorId"]) for tmp in j["authors"] if tmp["authorId"] is not None]))
        j["citing_authors"] = get_citing_authors(j["citations"], int(j["year"]))
        del j["citations"]
        j["cited_authors"] = list(set([int(author["authorId"]) for ref in j["references"] for author in ref["authors"] if author["authorId"] is not None]))
        del j["references"]
        del j["paperId"]
        return j

    # See Semantic Scholar API docs: https://api.semanticscholar.org/api-docs/graph#tag/Paper-Data/operation/post_graph_get_papers
    batch_query(
        json_save_path=os.path.join(DATA_DIR, "papers.json"),
        query_ids=[f"ARXIV:{id}" for id in kaggle_data["id"]],
        batch_size=20,
        query_fields="year,referenceCount,isOpenAccess,fieldsOfStudy,s2FieldsOfStudy,publicationTypes,authors,citations.year,citations.authors,references.authors",
        query_url="https://api.semanticscholar.org/graph/v1/paper/batch",
        process_response_f=process_paper_response
    )


def prepare_authors_data():
    papers_path = os.path.join(DATA_DIR, "papers.json")
    with open(papers_path) as f:
        papers = json.load(f)
    citing_authors = set()
    for paper in papers.values():
        for citing_author in paper["citing_authors"]:
            citing_authors.add(citing_author)
    print(f"{len(papers)} papers have {len(citing_authors)} citing authors")

    def process_author_response(j: json):
        papers = [
            {
                "year": paper["year"],
                "fieldsOfStudy": paper["fieldsOfStudy"],
                "s2FieldsOfStudy": list(set([tmp["category"] for tmp in paper["s2FieldsOfStudy"] if tmp["category"] is not None]))
            }
            for paper in j["papers"] if paper["year"] is not None
        ]
        return {
            "papers": papers
        }

    # See Semantic Scholar API docs: https://api.semanticscholar.org/api-docs/graph#tag/Author-Data/operation/post_graph_get_authors
    batch_query(
        json_save_path=os.path.join(DATA_DIR, "authors.json"),
        query_ids=list(citing_authors),
        batch_size=100,
        query_fields="papers.year,papers.fieldsOfStudy,papers.s2FieldsOfStudy",
        query_url="https://api.semanticscholar.org/graph/v1/author/batch",
        process_response_f=process_author_response
    )

prepare_papers_data()
prepare_authors_data()
