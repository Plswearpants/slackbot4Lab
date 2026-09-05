"""Published instrument descriptions, transcribed from the lab's own pages.

These are used verbatim as the abstract for each instrument. The lab's prose is
better than anything a model would write from Slack: the Createc page states
that the Besocke head's three-legged design resists thermal drift *and* limits
Z-range, and generated copy keeps the benefit while dropping the caveat.

Only the instruments with an indexed channel are here; the JEOL has no channel.
Each carries its source URL so a reader can see which part of a profile is
published lab copy and which the bot inferred.
"""

INSTRUMENT_SEEDS: dict[str, dict[str, str]] = {
    "createc": {
        "source": "https://lair.phas.ubc.ca/instruments/createc-4-k-uhv-stm-afm/",
        "abstract": (
            "The Createc is a 4-Kelvin Scanning Tunnelling Microscope manufactured by "
            "the company of the same name, used for atomic imaging in the group's work "
            "on material structures and behaviours.\n\n"
            "It is composed of two primary sections, a preparation chamber and an STM "
            "chamber, both UHV to decrease the rate of sample degradation. The "
            "preparation chamber is fitted with a suite of surface science preparation "
            "tools: an Ar+ sputtering gun, a low-energy electron diffraction setup, a "
            "quadrupole mass spectrometer, and an electron beam tip heating tool for tip "
            "annealing.\n\n"
            "The STM chamber houses the STM head, fashioned on the Besocke-type STM-AFM "
            "head model. Its 3-legged design confers some thermal drift resistance, "
            "useful during long-term spectroscopy measurements, though the design limits "
            "the Z-axis motion range. A manipulating arm transfers sample crystals and "
            "STM tips between the two chambers."
        ),
    },
    "omicron": {
        "source": (
            "https://lair.phas.ubc.ca/instruments/"
            "omicron-4-k-stm-afm-with-optical-access-omi/"
        ),
        "abstract": (
            "The Omicron is the LAIR's newest scanning probe microscope: an ultrahigh "
            "vacuum, low temperature scanning tunnelling and atomic force microscope "
            "with optical access to the tip-sample junction. It has a base temperature "
            "around 4 K, with stable imaging at 77 K and room temperature, and a base "
            "pressure below 5 x 10-11 mbar. The probe sensor can be either a standard "
            "STM tip or a QPlus sensor, the QPlus allowing simultaneous AFM and STM "
            "measurement. It is located in an ultra-low vibration space, the Omega pod."
        ),
    },
    "tesla": {
        "source": (
            "https://lair.phas.ubc.ca/instruments/"
            "joule-thomson-stm-afm-with-arpes-tesla/"
        ),
        "abstract": (
            "Tesla is a Joule-Thomson STM/AFM with integrated ARPES, commissioned in "
            "2019 and housed at 61 Brimacombe. It operates at 1 K with a 170-hour LHe "
            "holding time, enabling undisturbed long measurements such as quasiparticle "
            "interference studies extending up to one week at improved resolution. A dry "
            "superconducting split-pair 1D magnet provides vertical fields up to 3 "
            "Tesla.\n\n"
            "Both standard STM tips and QPlus AFM tips work on the JT platform, enabling "
            "spin-polarised STM and magnetic force microscopy when combined with the "
            "field. A bidirectional transfer chamber connects the JT STM/AFM to an ARPES "
            "system, permitting analysis of the very same sample by complementary "
            "techniques; the ARPES side is optimised for quick high-resolution "
            "photoemission and has a cooling/heating stage spanning -130 to 830 degrees C."
        ),
    },
    "beast": {
        "source": (
            "https://lair.phas.ubc.ca/instruments/"
            "high-magnetic-field-ultra-low-temperature-stm-beast/"
        ),
        "abstract": (
            "The Beast is a homebuilt scanning tunnelling microscope designed in the late "
            "2000s and constructed in the early 2010s, combining high magnetic field with "
            "ultra-low temperature operation. A dilution refrigerator reaches temperatures "
            "at the STM head of about 100 mK. A vector magnet allows up to 7 T in the "
            "z-direction and 2 T into the x-y plane of the sample, so the sample's magnetic "
            "moment, spin orientation and magnetic anisotropy can be measured with high "
            "spatial resolution.\n\n"
            "It sits on an 80-metric-ton concrete block in one of the quietest experimental "
            "rooms in North America. Research focuses on superconductors, magnetic "
            "materials, 2D materials and nanoscale devices."
        ),
    },
    "4probe": {
        "source": "",
        "abstract": (
            "The 4-probe is a four-tip scanning tunnelling microscope under construction "
            "by the group, not yet a running instrument. It is built around a "
            "Joule-Thomson cryostat with nested radiation shields at 4 K, 20 K, 77 K and "
            "220 K, with Nanonis control replacing the Createc software used elsewhere, "
            "and a custom switch box routing TTL signals between the probes."
        ),
    },
}
