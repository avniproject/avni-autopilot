---
rule_kind: visitScheduleRule
intent: "reschedule the next Addiction Follow-up a month after the cancellation date with a 15-day window — only if the participant hasn't exited and we're under the 3-per-baseline-or-endline quota"
entity_param: programEncounter
encounter_types: ["Addiction Follow-up"]
concepts: []
source_org: "ADSR"
---
```js
"use strict";
({ params, imports }) => {
  const programEncounter = params.entity;
  const moment = imports.moment;
  const scheduleBuilder = new imports.rulesConfig.VisitScheduleBuilder({ programEncounter });

  const hasExitedProgram = (programEncounter) => programEncounter.programEnrolment.programExitDateTime;

  if (!hasExitedProgram(programEncounter)) {
    const encounters = programEncounter.programEnrolment.getEncounters();

    const numberOfBaselineEncountersCompleted = encounters.filter(enc => 
      enc.encounterType.name === "Annual Visit - Baseline" && 
      enc.encounterDateTime != null
    ).length;

    const numberOfEndlineEncountersCompleted = encounters.filter(enc => 
      enc.encounterType.name === "Annual Visit - Endline" && 
      enc.encounterDateTime != null
    ).length;

    const numberOfAddictionFollowupEncoutnersCompleted = encounters.filter(enc =>
      enc.encounterType.name === "Addiction Follow-up"
    ).length;

    if ((numberOfBaselineEncountersCompleted + numberOfEndlineEncountersCompleted) * 3 > numberOfAddictionFollowupEncoutnersCompleted) {
      const earliestDate = moment(programEncounter.cancelDateTime).add(1,'month').toDate();
      const maxDate = moment(earliestDate).add(15, 'days').toDate();
      scheduleBuilder.add({
        name: "Addiction Follow-up",
        encounterType: "Addiction Follow-up",
        earliestDate,
        maxDate
      });
    }
  }

  return scheduleBuilder.getAll();
};
```
