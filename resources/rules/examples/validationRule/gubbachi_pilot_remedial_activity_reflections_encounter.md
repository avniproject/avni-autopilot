---
rule_kind: validationRule
intent: block saving when the participants reported across completed and remedial
  groups don't cover every member of the group-subject
entity_param: programEncounter
encounter_types: []
concepts:
- e0d1c2b3-a4f5-6789-3456-789012345007
source_org: Gubbachi Pilot
---
```js
"use strict";
 ({params, imports}) => {
  const programEncounter = params.entity;
  const _ = imports.lodash;
  const validationResults = [];
  
  if(programEncounter.programEnrolment.individual && programEncounter.programEnrolment.individual.groupSubjects) {
  let membersubjects = programEncounter.programEnrolment.individual.groupSubjects.filter(gs => !gs.voided);
  const totalStudents = membersubjects.length;
  
  let studentsCompletedActivities = programEncounter.getObservationValue('e0d1c2b3-a4f5-6789-3456-789012345007') || [];
  const completedCount = Array.isArray(studentsCompletedActivities) ? studentsCompletedActivities.length : 0;
  
  let groupedObservation = programEncounter.findGroupedObservation('c0d1e2f3-a4b5-6789-3456-789012345003');
  
  let remedialStudentsCount = 0;
  
  if(groupedObservation && groupedObservation.length > 0) {
  groupedObservation.forEach((group) => {
  let studentObs = group.groupObservations.find(obs => obs.concept.uuid === '8fb8200c-3ec8-408c-a67d-fda6e2a41f1a');
  let reasonObs = group.groupObservations.find(obs => obs.concept.uuid === 'f1e2d3c4-b5a6-7890-4567-890123456005');
  
  if(studentObs && studentObs.valueJSON && studentObs.valueJSON.answer && 
  reasonObs && reasonObs.valueJSON && reasonObs.valueJSON.answer) {
  
  const studentsInGroup = studentObs.valueJSON.answer;
  const studentCount = Array.isArray(studentsInGroup) ? studentsInGroup.length : 1;
  remedialStudentsCount += studentCount;
  }
  });
  }
  
  const accountedStudents = completedCount + remedialStudentsCount;
  
  if(accountedStudents < totalStudents) {
  const missingCount = totalStudents - accountedStudents;
  
  let validationError = imports.common.createValidationError(
  `Please account for all students. ${missingCount} student(s) not accounted for in completed or remedial activities.`
  );
  validationResults.push(validationError);
  }
  }
  
  return validationResults;
 };
```
