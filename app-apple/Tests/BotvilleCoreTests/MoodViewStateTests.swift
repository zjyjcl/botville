// Copyright 2026 botville contributors
// SPDX-License-Identifier: Apache-2.0

import Foundation
import XCTest

@testable import BotvilleCore

final class MoodViewStateTests: XCTestCase {
    func testOwnerFactoryForEveryMoodVisibilityAndMissingMood() {
        for mood in PersonaMood.allCases.map(Optional.some) + [nil] {
            for isPublic in [false, true] {
                let presentation = PersonaMoodPresentation(ownerState: PersonaState(
                    personaID: "owner", mood: mood, moodPublic: isPublic))
                XCTAssertEqual(presentation.mood, mood)
                XCTAssertEqual(presentation.privateMarkerText,
                               mood != nil && !isPublic ? "仅自己可见" : nil)
            }
        }
    }

    func testCounterpartFactoryNeverIncludesOwnerPrivateMarker() {
        for mood in PersonaMood.allCases.map(Optional.some) + [nil] {
            let presentation = PersonaMoodPresentation(counterpartMood: mood)
            XCTAssertEqual(presentation.mood, mood)
            XCTAssertNil(presentation.privateMarkerText)
        }
    }

    func testCounterpartDecodedMoodIncludingUnknownNullAndMissingNeverShowsMarker() throws {
        let cases = PersonaMood.allCases.map { (",\"mood\":\"\($0.rawValue)\"", Optional($0)) }
            + [(",\"mood\":\"future\"", nil), (",\"mood\":null", nil), ("", nil)]
        for (field, expected) in cases {
            let data = Data("{\"id\":\"p\",\"display_name\":\"AI\",\"status\":\"active\"\(field)}".utf8)
            let detail = try JSONDecoder().decode(PersonaDetail.self, from: data)
            let presentation = PersonaMoodPresentation(counterpartMood: detail.mood)
            XCTAssertEqual(presentation.mood, expected)
            XCTAssertNil(presentation.privateMarkerText)
        }
    }

    func testOwnerUnknownAndNullMoodDecodeToNoPrivateMarker() throws {
        for moodJSON in ["null", "{\"label\":\"future\"}"] {
            for isPublic in [false, true] {
                let data = Data("{\"persona_id\":\"p\",\"mood\":\(moodJSON),\"mood_public\":\(isPublic)}".utf8)
                let state = try JSONDecoder().decode(PersonaState.self, from: data)
                let presentation = PersonaMoodPresentation(ownerState: state)
                XCTAssertNil(presentation.mood)
                XCTAssertNil(presentation.privateMarkerText)
            }
        }
    }
}
