---
rule_kind: formElementRule
intent: show this field only when a certain number of days have passed since a previously
  recorded date (e.g. after 126 days since LMP)
entity_param: programEncounter
encounter_types: []
concepts:
- 1cc6fd5d-1359-483e-a971-4bf36e34a72d
source_org: JSCS
field_name: Anomalies scan at 18 to 20 weeks completed?
kind: visibility
---
```js
'use strict';
({ params, imports }) => {
  const programEncounter = params.entity;
  const moment = imports.moment;
  const formElement = params.formElement;
  let visibility = false; // Default: Question is not visible

  // Fetch LMP Date from Enrolment
  const lmpDate = programEncounter.programEnrolment.findLatestObservationInEntireEnrolment('1cc6fd5d-1359-483e-a971-4bf36e34a72d').getValue();

  if (lmpDate) {
    const today = moment(); // Get today's date
    const lmpMoment = moment(lmpDate, "YYYY-MM-DD"); // Convert LMP date to moment object
    const daysDifference = today.diff(lmpMoment, 'days'); // Calculate difference in days

    // Hide the question if the difference is greater than 84 days
    if (daysDifference > 126) {
      visibility = true;
    }
  }

  return new imports.rulesConfig.FormElementStatus(formElement.uuid, visibility);
};
```
