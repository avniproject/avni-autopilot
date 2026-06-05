---
rule_kind: visitScheduleRule
intent: "schedule the first SCD Followup Jan 2024 visit 90 days later with a 25-day window, unless one already exists for this enrolment"
entity_param: programEncounter
encounter_types: ["SCD Followup Jan 2024"]
concepts: []
source_org: "JSCS"
---
```js
//SAMPLE RULE EXAMPLE
"use strict";
({ params, imports }) => {
  const programEncounter = params.entity;
  const scheduleBuilder = new imports.rulesConfig.VisitScheduleBuilder({
    programEncounter
  });
  const moment = imports.moment;
  
  const whetherScdFollowupAlreadyScheduled = programEncounter.programEnrolment.hasEncounter('SCD Followup Jan 2024');
  
  if ( !whetherScdFollowupAlreadyScheduled ) {
  
    const earliestDate = moment(programEncounter.encounterDateTime).add(90, "days").toDate();
    const maxDate = moment(earliestDate).add(25, "days").toDate();
    scheduleBuilder.add({ name: "SCD Followup Jan 2024", encounterType: "SCD Followup Jan 2024", earliestDate, maxDate });
  
  };
  
  return scheduleBuilder.getAll();
};
```
