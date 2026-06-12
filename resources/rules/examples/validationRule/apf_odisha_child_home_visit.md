---
rule_kind: validationRule
intent: block saving when the visit is overdue or its date is before the scheduled
  date
entity_param: programEncounter
encounter_types: []
concepts: []
source_org: APF Odisha
---
```js
"use strict";
 ({params, imports}) => {
  const programEncounter = params.entity;
  const moment = imports.moment;
  const validationResults = [];
  
  const isMaxVisitDateTimeGreaterThanToday = moment(programEncounter.maxVisitDateTime).isBefore(moment(), 'day');
 

  if (isMaxVisitDateTimeGreaterThanToday) {
  validationResults.push(imports.common.createValidationError("You cannot complete an overdue visit. Please cancel this visit."));  
  }
  // Get schedule date (earliest visit date)
  const scheduleDate = programEncounter.earliestVisitDateTime;
  
  // Check if encounter date is before schedule month
  const isBeforeScheduleMonth = moment(programEncounter.encounterDateTime)
  .isBefore(moment(scheduleDate));
 

  // Check if encounter date is after schedule month
  const isAfterScheduleMonth = moment(programEncounter.encounterDateTime)
  .isAfter(moment(scheduleDate).endOf('month'));
 

  if (isBeforeScheduleMonth) {
  validationResults.push(imports.common.createValidationError("Cannot fill this form before the scheduled date"));
  }
 

  
  
  
  return validationResults;
 };
```
