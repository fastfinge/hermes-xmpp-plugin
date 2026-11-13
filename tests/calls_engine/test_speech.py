from calls.speech import clean, sentences, split_hangup


def test_clean_rules() -> None:
    text = "Use **this** [guide](https://x.io/a) or https://x.io/b.\n"
    text += "```py\nprint(1)\n```\n- item `one`"
    spoken = clean(text)
    assert "http" not in spoken
    assert "guide" in spoken
    assert "a link" in spoken
    assert "print" not in spoken
    assert "code omitted" in spoken
    assert "item one" in spoken
    assert "*" not in spoken
    assert "`" not in spoken


def test_sentences_merge_short_pieces() -> None:
    parts = sentences("Yes. It is sunny and warm outside today. No rain at all, and calm wind.")
    assert parts == ["Yes. It is sunny and warm outside today.", "No rain at all, and calm wind."]
    assert sentences("It is sunny and warm outside today. Yes!") == [
        "It is sunny and warm outside today. Yes!"
    ]


def test_single_short_reply_stays_whole() -> None:
    assert sentences("Okay.") == ["Okay."]
    assert sentences("") == []


def test_hangup_marker() -> None:
    assert split_hangup("Bye now. [[hangup]]") == ("Bye now.", True)
    assert split_hangup("Still here.") == ("Still here.", False)


def test_long_first_sentence_is_split_at_a_clause() -> None:
    text = (
        "Well, since you asked about the weather this afternoon, I would say it looks clear. Enjoy!"
    )
    parts = sentences(text)
    assert parts[0] == "Well, since you asked about the weather this afternoon,"
    assert " ".join(parts) == text


def test_stock_phrases_from_noise_are_phantoms_only_when_short() -> None:
    from calls.speech import is_phantom

    assert is_phantom("Thank you.", 1.6)
    assert is_phantom("Thanks for watching!", 2.0)
    assert not is_phantom("Thank you.", 4.0)  # a long utterance is real speech
    assert not is_phantom("Yes, thank you very much", 1.5)
    assert is_phantom("Thank you. Thank you. Thank you. Thank you. Thank you.", 4.3)
    assert not is_phantom("Thank you. Goodbye.", 4.0)
    assert is_phantom("♪♪♪♪", 2.8)
    assert is_phantom("[Music]", 9.0)
    assert not is_phantom("Okay.", 1.0)
    # a lone "bye" (or the "by" it sounds like) never says goodbye, however slowly
    assert is_phantom("Bye.", 1.0)
    assert is_phantom("Bye", 6.0)
    assert is_phantom("By.", 4.0)
    assert not is_phantom("Bye bye.", 1.5)
    assert not is_phantom("Bye. Bye.", 1.5)  # speech-to-text punctuates a double bye like this
    assert not is_phantom("Bye. Bye. Bye.", 4.0)
    assert not is_phantom("bye, bye", 1.5)
    assert is_phantom("Thank you. Bye.", 3.5)  # a mix of stock phrases is still noise
    assert is_phantom("Bye. Thank you. Thank you.", 5.0)
    assert not is_phantom("Goodbye.", 1.0)
    assert not is_phantom("Okay, thanks, bye.", 2.0)
