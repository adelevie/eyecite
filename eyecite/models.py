import re
from collections import UserString
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, ClassVar, Dict, List, Optional, Sequence, Union


@dataclass(eq=True, frozen=True)
class Reporter:
    """Class for top-level reporters in reporters_db, like "S.W." """

    short_name: str
    name: str
    cite_type: str
    is_scotus: bool = False

    def __post_init__(self):
        if (
            self.cite_type == "federal" and "supreme" in self.name.lower()
        ) or "scotus" in self.cite_type.lower():
            # use setattr because this class is frozen
            object.__setattr__(self, "is_scotus", True)


@dataclass(eq=True, frozen=True)
class Edition:
    """Class for individual editions in reporters_db,
    like "S.W." and "S.W.2d"."""

    reporter: Reporter
    short_name: str
    start: Optional[datetime]
    end: Optional[datetime]

    def includes_year(
        self,
        year: int,
    ) -> bool:
        """Return True if edition contains cases for the given year."""
        return (
            year <= datetime.now().year
            and (self.start is None or self.start.year <= year)
            and (self.end is None or self.end.year >= year)
        )


@dataclass(eq=True, unsafe_hash=True)
class CitationBase:
    """Base class for objects returned by get_citations()."""

    token: "Token"  # token this citation came from
    index: int  # index of _token in the token list

    def matched_text(self):
        """Text that identified this citation, such as '1 U.S. 1' or 'Id.'"""
        return str(self.token)


@dataclass(eq=True, unsafe_hash=True)
class CaseCitation(CitationBase):
    """Convenience class which represents a single citation found in a
    document.
    """

    # Core data.
    reporter: str
    page: Optional[str] = None
    volume: Optional[str] = None

    # Set during disambiguation.
    # For a citation to F.2d, the canonical reporter is F.
    canonical_reporter: Optional[str] = None

    # Supplementary data, if possible.
    extra: Optional[str] = None
    defendant: Optional[str] = None
    plaintiff: Optional[str] = None
    court: Optional[str] = None
    year: Optional[int] = None

    # The reporter found in the text is often different from the reporter
    # once it's normalized. We need to keep the original value so we can
    # linkify it with a regex.
    reporter_found: Optional[str] = None

    # Editions that might match this reporter string
    exact_editions: Sequence[Edition] = field(default_factory=tuple)
    variation_editions: Sequence[Edition] = field(default_factory=tuple)
    all_editions: Sequence[Edition] = field(default_factory=tuple)
    edition_guess: Optional[Edition] = None

    def __post_init__(self):
        """Make iterables into tuples to make sure we're hashable."""
        self.exact_editions = tuple(self.exact_editions)
        self.variation_editions = tuple(self.variation_editions)
        self.all_editions = tuple(self.exact_editions) + tuple(
            self.variation_editions
        )

    def as_regex(self):
        pass

    def base_citation(self):
        return "%s %s %s" % (self.volume, self.reporter, self.page)

    def __repr__(self):
        print_string = self.base_citation()
        if self.defendant:
            print_string = " ".join([self.defendant, print_string])
            if self.plaintiff:
                print_string = " ".join([self.plaintiff, "v.", print_string])
        if self.extra:
            print_string = " ".join([print_string, self.extra])
        if self.court and self.year:
            paren = "(%s %d)" % (self.court, self.year)
        elif self.year:
            paren = "(%d)" % self.year
        elif self.court:
            paren = "(%s)" % self.court
        else:
            paren = ""
        print_string = " ".join([print_string, paren])
        return print_string

    def guess_edition(self):
        """Set canonical_reporter, edition_guess, and reporter."""
        # Use exact matches if possible, otherwise try variations
        editions = self.exact_editions or self.variation_editions
        if not editions:
            return

        # Attempt resolution by date
        if len(editions) > 1 and self.year:
            editions = [e for e in editions if e.includes_year(self.year)]

        if len(editions) == 1:
            self.edition_guess = editions[0]
            self.canonical_reporter = editions[0].reporter.short_name
            self.reporter = editions[0].short_name

    def guess_court(self):
        """Set court based on reporter."""
        if not self.court and any(
            e.reporter.is_scotus for e in self.all_editions
        ):
            self.court = "scotus"


@dataclass(eq=True, unsafe_hash=True)
class FullCaseCitation(CaseCitation):
    """Convenience class which represents a standard, fully named citation,
    i.e., the kind of citation that marks the first time a document is cited.

    Example: Adarand Constructors, Inc. v. Peña, 515 U.S. 200, 240
    """

    def as_regex(self):
        return r"%s(\s+)%s(\s+)%s(\s?)" % (
            self.volume,
            re.escape(self.reporter_found),
            re.escape(self.page),
        )


@dataclass(eq=True, unsafe_hash=True)
class ShortCaseCitation(CaseCitation):
    """Convenience class which represents a short form citation, i.e., the kind
    of citation made after a full citation has already appeared. This kind of
    citation lacks a full case name and usually has a different page number
    than the canonical citation.

    Example 1: Adarand, 515 U.S., at 241
    Example 2: Adarand, 515 U.S. at 241
    Example 3: 515 U.S., at 241
    """

    # Like a Citation object, but we have to guess who the antecedent is
    # and the page number is non-canonical
    antecedent_guess: Optional[str] = None

    def __repr__(self):
        print_string = "%s, %s %s, at %s" % (
            self.antecedent_guess,
            self.volume,
            self.reporter,
            self.page,
        )
        return print_string

    def as_regex(self):
        return r"%s(\s+)%s(\s+)%s(,?)(\s+)at(\s+)%s(\s?)" % (
            re.escape(self.antecedent_guess),
            self.volume,
            re.escape(self.reporter_found),
            re.escape(self.page),
        )


@dataclass(eq=True, unsafe_hash=True)
class SupraCitation(CitationBase):
    """Convenience class which represents a 'supra' citation, i.e., a citation
    to something that is above in the document. Like a short form citation,
    this kind of citation lacks a full case name and usually has a different
    page number than the canonical citation.

    Example 1: Adarand, supra, at 240
    Example 2: Adarand, 515 supra, at 240
    Example 3: Adarand, supra, somethingelse
    Example 4: Adarand, supra. somethingelse
    """

    # Like a Citation object, but without knowledge of the reporter or the
    # volume. Only has a guess at what the antecedent is.
    antecedent_guess: Optional[str] = None
    page: Optional[str] = None
    volume: Optional[str] = None

    def __repr__(self):
        print_string = "%s supra, at %s" % (self.antecedent_guess, self.page)
        return print_string

    def as_regex(self):
        if self.volume:
            regex = r"%s(\s+)%s(\s+)supra" % (
                re.escape(self.antecedent_guess),
                self.volume,
            )
        else:
            regex = r"%s(\s+)supra" % re.escape(self.antecedent_guess)

        if self.page:
            regex += r",(\s+)at(\s+)%s" % re.escape(self.page)

        return regex + r"(\s?)"


@dataclass(eq=True, unsafe_hash=True)
class IdCitation(CitationBase):
    """Convenience class which represents an 'id' or 'ibid' citation, i.e., a
    citation to the document referenced immediately prior. An 'id' citation is
    unlike a regular citation object since it has no knowledge of its reporter,
    volume, or page. Instead, the only helpful information that this reference
    possesses is a record of the tokens after the 'id' token. Those tokens
    enable us to build a regex to match this citation later.

    Example: "... foo bar," id., at 240
    """

    after_tokens: Optional["Tokens"] = None
    # Whether the "after tokens" comprise a page number
    has_page: bool = False

    def __repr__(self):
        print_string = "%s %s" % (self.token, self.after_tokens)
        return print_string

    def as_regex(self):
        # This works by matching only the Id. token that precedes the "after
        # tokens" we collected earlier.

        # Whitespace regex explanation:
        #  \s matches any whitespace character
        #  </?\w+> matches any HTML tag
        #  , matches a comma
        #  The whole thing matches greedily, saved into a single group
        whitespace_regex = r"((?:\s|</?\w+>|,)*)"

        # Start with a matching group for any whitespace
        template = whitespace_regex

        # Add the id_token
        template += re.escape(str(self.token))

        # Add a matching group for any whitespace
        template += whitespace_regex

        # Add all the "after tokens", with whitespace groups in between
        template += whitespace_regex.join(
            [re.escape(t) for t in self.after_tokens]
        )

        # Add a final matching group for any non-HTML whitespace at the end
        template += r"(\s?)"

        return template


@dataclass(eq=True, unsafe_hash=True)
class NonopinionCitation(CitationBase):
    """Convenience class which represents a citation to something that we know
    is not an opinion. This could be a citation to a statute, to the U.S. code,
    the U.S. Constitution, etc.

    Example 1: 18 U.S.C. §922(g)(1)
    Example 2: U. S. Const., Art. I, §8
    """

    pass


@dataclass(eq=True, frozen=True)
class Token(UserString):
    """Base class for special tokens. For performance, this isn't used
    for generic words."""

    data: str

    @classmethod
    def from_match(cls, m, extra):
        """Return a token object based on a regular expression match.
        This gets called by TokenExtractor. By default, just use the
        entire matched string."""
        return cls(m[0])


# For performance, lists of tokens can include either Token subclasses
# or bare strings (the typical case of words that aren't
# related to citations)
TokenOrStr = Union[Token, str]
Tokens = Sequence[TokenOrStr]


@dataclass(eq=True, frozen=True)
class CitationToken(Token):
    """ String matching a citation regex. """

    volume: str
    reporter: str
    page: str
    exact_editions: Sequence[Edition] = field(default_factory=tuple)
    variation_editions: Sequence[Edition] = field(default_factory=tuple)
    short: bool = False

    def __post_init__(self):
        """Make iterables into tuples to make sure we're hashable."""
        # use setattr because this class is frozen
        object.__setattr__(self, "exact_editions", tuple(self.exact_editions))
        object.__setattr__(
            self, "variation_editions", tuple(self.variation_editions)
        )

    @classmethod
    def from_match(cls, m, extra):
        """Citation regex matches have volume, reporter, and page match groups
        in their regular expressions, and "exact_editions" and
        "variation_editions" in their extra config. Pass all of that through
        to the constructor."""
        return cls(
            m[0],
            **m.groupdict(),
            **extra,
        )


@dataclass(eq=True, frozen=True)
class SectionToken(Token):
    """ Word containing a section symbol. """

    pass


@dataclass(eq=True, frozen=True)
class SupraToken(Token):
    """ Word matching "supra" with or without punctuation. """

    @classmethod
    def from_match(cls, m, extra):
        """Only use the captured part of the match to omit whitespace."""
        return cls(m[1])


@dataclass(eq=True, frozen=True)
class IdToken(Token):
    """ Word matching "id" or "ibid". """

    @classmethod
    def from_match(cls, m, extra):
        """Only use the captured part of the match to omit whitespace."""
        return cls(m[1])


@dataclass(eq=True, frozen=True)
class StopWordToken(Token):
    """ Word matching one of the STOP_TOKENS. """

    stop_word: str
    stop_tokens: ClassVar[Sequence[str]] = (
        "v",
        "re",
        "parte",
        "denied",
        "citing",
        "aff'd",
        "affirmed",
        "remanded",
        "see",
        "granted",
        "dismissed",
    )

    @classmethod
    def from_match(cls, m, extra):
        """m[1] is the captured part of the match, including punctuation.
        m[2] is just the underlying stopword like 'v', useful for comparison.
        """
        return cls(m[1], m[2].lower())


@dataclass
class TokenExtractor:
    """Object to extract all matches from a given string for the given regex,
    and then to return Token objects for all matches."""

    regex: str
    constructor: Callable
    extra: Dict = field(default_factory=dict)
    flags: int = 0
    strings: List = field(default_factory=list)

    def get_matches(self, text):
        """Return match objects for all matches in text."""
        return self.compiled_regex.finditer(text)

    def get_token(self, m):
        """For a given match object, return a Token."""
        return self.constructor(m, self.extra)

    def __hash__(self):
        """This needs to be hashable so we can remove redundant
        extractors returned by the pyahocorasick filter."""
        return hash(repr(self))

    @property
    def compiled_regex(self):
        """Cache compiled regex as a property."""
        if not hasattr(self, "_compiled_regex"):
            self._compiled_regex = re.compile(self.regex, flags=self.flags)
        return self._compiled_regex


@dataclass
class ExtractorMatch:
    """Data for a single match found by a TokenExtractor."""

    extractor: TokenExtractor
    m: Optional[re.Match]
    start: int
    end: int
