from bs4 import BeautifulSoup, Tag, Comment
import pyperclip

# ==========================================
# CONFIG
# ==========================================

MAX_DEPTH = 20
MAX_SIMILAR_SIBLINGS = 4


# ==========================================
# DEPTH TRIMMING
# ==========================================

def trim_depth(node, current_depth, max_depth):
    if not isinstance(node, Tag):
        return

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
# REMOVE ALL STYLE ATTRIBUTES
# ==========================================

def remove_style_attributes(soup):
    """Remove the 'style' attribute from every tag in the soup."""
    for tag in soup.find_all():
        if tag.has_attr('style'):
            del tag['style']


# ==========================================
# REMOVE EMPTY TAGS
# ==========================================

def remove_empty_tags(soup):
    changed = True
    while changed:
        changed = False
        for tag in soup.find_all():
            # A tag is considered empty if it has no attributes,
            # no text content, and no child tags.
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

    # 1. Remove comments
    remove_comments(soup)

    # 2. Strip all style="..." attributes (only attribute removal)
    remove_style_attributes(soup)

    # 3. Trim depth – no attribute cleaning inside
    root_tags = [node for node in soup.contents if isinstance(node, Tag)]
    for root in root_tags:
        trim_depth(root, 0, MAX_DEPTH)

    # 4. Limit identical sibling groups
    limit_repeated_siblings(soup)

    # 5. Remove leftover truly empty tags
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
    reduction = (1 - cleaned_len / max(original_len, 1)) * 100

    print()
    print(f"Original chars : {original_len:,}")
    print(f"Cleaned chars  : {cleaned_len:,}")
    print(f"Reduction      : {reduction:.1f}%")
    print()
    print("Cleaned HTML copied to clipboard.")


if __name__ == "__main__":
    main()