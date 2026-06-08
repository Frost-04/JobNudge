from bs4 import BeautifulSoup, Tag, Comment
import pyperclip

# ==========================================
# CONFIG
# ==========================================

MAX_DEPTH = 6

KEEP_ATTRS = {
    "id",
    "class",
    "name",
    "type",
    "href",
    "role",
    "aria-label",
    "placeholder",
}

KEEP_DATA_ATTRS = {
    "data-ph-at-id",
    "data-ph-at-text",
    "data-ph-at-facetkey",
}

MAX_SIMILAR_SIBLINGS = 3


# ==========================================
# ATTRIBUTE CLEANUP
# ==========================================

def clean_attributes(tag):
    attrs_to_keep = {}

    for key, value in tag.attrs.items():

        if key in KEEP_ATTRS:
            attrs_to_keep[key] = value
            continue

        if key in KEEP_DATA_ATTRS:
            attrs_to_keep[key] = value
            continue

    tag.attrs = attrs_to_keep


# ==========================================
# DEPTH TRIMMING
# ==========================================

def trim_depth(node, current_depth, max_depth):
    if not isinstance(node, Tag):
        return

    clean_attributes(node)

    if current_depth >= max_depth:
        node.clear()
        return

    for child in list(node.children):
        if isinstance(child, Tag):
            trim_depth(child, current_depth + 1, max_depth)


# ==========================================
# REMOVE COMMENTS
# ==========================================

def remove_comments(soup):
    for comment in soup.find_all(
        string=lambda text: isinstance(text, Comment)
    ):
        comment.extract()


# ==========================================
# REMOVE EMPTY TAGS
# ==========================================

def remove_empty_tags(soup):
    changed = True

    while changed:
        changed = False

        for tag in soup.find_all():
            if (
                len(tag.attrs) == 0
                and not tag.get_text(strip=True)
                and not tag.find()
            ):
                tag.decompose()
                changed = True


# ==========================================
# DEDUPE REPEATED SIBLINGS
# ==========================================

def get_signature(tag):
    if not isinstance(tag, Tag):
        return None

    return (
        tag.name,
        tuple(sorted(tag.attrs.keys())),
        len(tag.find_all(recursive=False)),
    )


def limit_repeated_siblings(soup):
    for parent in soup.find_all():

        seen = {}

        for child in list(parent.find_all(recursive=False)):

            sig = get_signature(child)

            count = seen.get(sig, 0)

            if count >= MAX_SIMILAR_SIBLINGS:
                child.decompose()
            else:
                seen[sig] = count + 1


# ==========================================
# MAIN PROCESSING
# ==========================================

def process_html(html):

    soup = BeautifulSoup(html, "html.parser")

    remove_comments(soup)

    root_tags = [
        node
        for node in soup.contents
        if isinstance(node, Tag)
    ]

    for root in root_tags:
        trim_depth(root, 0, MAX_DEPTH)

    limit_repeated_siblings(soup)

    remove_empty_tags(soup)

    return soup.prettify()


# ==========================================
# MAIN
# ==========================================

def main():

    html = pyperclip.paste()

    if not html.strip():
        print("Clipboard is empty.")
        return

    cleaned = process_html(html)

    pyperclip.copy(cleaned)

    original_len = len(html)
    cleaned_len = len(cleaned)

    reduction = (
        (1 - cleaned_len / max(original_len, 1))
        * 100
    )

    print()
    print(f"Original chars : {original_len:,}")
    print(f"Cleaned chars  : {cleaned_len:,}")
    print(f"Reduction      : {reduction:.1f}%")
    print()
    print("Cleaned HTML copied to clipboard.")


if __name__ == "__main__":
    main()